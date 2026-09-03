"""
ComfyUI-QueueControl — Pause/unpause + priority system for the prompt queue.
Priority 0-8: normal (lower runs first). 9: on hold (never runs).
Only one priority-0 item allowed; setting a new 0 bumps the old one to 1.
New jobs enter at priority 5.
"""

import asyncio
import copy
import heapq
import json
import logging
import os
import threading
import time
import types
import uuid
from aiohttp import web
from server import PromptServer

LOG_PREFIX = "[QueueControl]"
_EXTENSION_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTO_BACKUP_PATH = os.path.join(_EXTENSION_DIR, "auto_backup.json")
logging.info(f"{LOG_PREFIX} Loading extension...")


# ── Pause state ───────────────────────────────────────────────────
_paused = False
_pause_lock = threading.Lock()


def is_paused():
    with _pause_lock:
        return _paused


def set_paused(value: bool):
    global _paused
    with _pause_lock:
        _paused = value
    try:
        pq = PromptServer.instance.prompt_queue
        with pq.not_empty:
            pq.not_empty.notify()
    except Exception as e:
        logging.warning(f"{LOG_PREFIX} Could not notify queue: {e}")
    logging.info(f"{LOG_PREFIX} Queue {'PAUSED' if value else 'RESUMED'}")


# ── Priority helpers ──────────────────────────────────────────────
PRIORITY_MULTIPLIER = 1_000_000
DEFAULT_PRIORITY = 5
HOLD_PRIORITY = 9
_sequence_counter = 0
_counter_lock = threading.Lock()


def _next_sequence():
    global _sequence_counter
    with _counter_lock:
        _sequence_counter += 1
        return _sequence_counter


def _make_sort_key(priority, seq=None):
    if seq is None:
        seq = _next_sequence()
    return priority * PRIORITY_MULTIPLIER + seq


def _get_priority(item):
    return int(item[0] // PRIORITY_MULTIPLIER)


def _get_sequence(item):
    return int(item[0] % PRIORITY_MULTIPLIER)


# ── Queue patches ─────────────────────────────────────────────────
_patch_installed = False


def _install_patch():
    global _patch_installed
    if _patch_installed:
        return True
    try:
        pq = PromptServer.instance.prompt_queue

        def _patched_get(self, timeout=None):
            with self.not_empty:
                while True:
                    if is_paused() or len(self.queue) == 0:
                        self.not_empty.wait(timeout=timeout)
                        if timeout is not None and (is_paused() or len(self.queue) == 0):
                            return None
                        continue
                    top = self.queue[0]
                    if _get_priority(top) >= HOLD_PRIORITY:
                        self.not_empty.wait(timeout=timeout)
                        if timeout is not None:
                            return None
                        continue
                    item = heapq.heappop(self.queue)
                    i = self.task_counter
                    self.currently_running[i] = copy.deepcopy(item)
                    self.task_counter += 1
                    self.server.queue_updated()
                    return (item, i)

        def _patched_put(self, item):
            # Negative or zero number means shift+click "send to front" — map to priority 0
            priority = 0 if item[0] <= 0 else DEFAULT_PRIORITY
            new_item = (_make_sort_key(priority),) + item[1:]
            with self.mutex:
                # Enforce only-one-zero rule
                if priority == 0:
                    for i, q_item in enumerate(self.queue):
                        if _get_priority(q_item) == 0:
                            old_seq = _get_sequence(q_item)
                            self.queue[i] = (_make_sort_key(1, old_seq),) + q_item[1:]
                    heapq.heapify(self.queue)
                heapq.heappush(self.queue, new_item)
                self.server.queue_updated()
                self.not_empty.notify()

        pq.get = types.MethodType(_patched_get, pq)
        pq.put = types.MethodType(_patched_put, pq)
        _patch_installed = True
        logging.info(f"{LOG_PREFIX} Queue patches installed successfully")
        return True
    except Exception as e:
        logging.error(f"{LOG_PREFIX} Queue patches FAILED: {e}")
        return False


_install_patch()


# ── Queue Label Node ──────────────────────────────────────────────
class QueueLabel:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "label": ("STRING", {"default": "", "multiline": False}),
            },
            "optional": {
                "info": ("*",),
                "info_idx": ("INT", {"default": 0, "min": 0, "max": 50}),
                "info2": ("*",),
                "info2_idx": ("INT", {"default": 0, "min": 0, "max": 50}),
                "info3": ("*",),
                "info3_idx": ("INT", {"default": 0, "min": 0, "max": 50}),
            },
        }

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "execute"
    CATEGORY = "QueueControl"

    def execute(self, label, info=None, info_idx=0, info2=None, info2_idx=0, info3=None, info3_idx=0):
        return {}


def _trace_value(prompt, ref, idx=0, depth=0):
    """Follow a connection reference and return the value at the given input index."""
    if depth > 10:
        return None
    if not isinstance(ref, list) or len(ref) < 2:
        return None
    node_id = str(ref[0])
    node_data = prompt.get(node_id)
    if not isinstance(node_data, dict):
        return None
    inputs = node_data.get("inputs", {})
    # Get all inputs in order
    input_list = list(inputs.items())
    if idx >= len(input_list):
        return None
    key, val = input_list[idx]
    if isinstance(val, list):
        # It's a connection — trace it (use index 0 for chained lookups)
        return _trace_value(prompt, val, 0, depth + 1)
    elif isinstance(val, (str, int, float)):
        if isinstance(val, str) and val.strip():
            return val.strip()
        elif isinstance(val, (int, float)):
            return str(val)
    return None


def _extract_label(prompt):
    if not isinstance(prompt, dict):
        return ""
    for node_id, node_data in prompt.items():
        if isinstance(node_data, dict) and node_data.get("class_type") == "QueueLabel":
            inputs = node_data.get("inputs", {})
            label = inputs.get("label", "")

            # Resolve all info fields
            info_parts = []
            for info_key, idx_key in (("info", "info_idx"), ("info2", "info2_idx"), ("info3", "info3_idx")):
                val = inputs.get(info_key, None)
                idx = inputs.get(idx_key, 0)
                if isinstance(idx, list):
                    idx = 0  # idx is a connection somehow — fall back to 0
                idx = int(idx) if idx else 0
                resolved = ""
                if isinstance(val, list):
                    r = _trace_value(prompt, val, idx)
                    if r:
                        resolved = r
                elif isinstance(val, (str, int, float)):
                    resolved = str(val).strip()
                if resolved:
                    info_parts.append(resolved)

            info_str = " | ".join(info_parts)

            if label and info_str:
                return f"{label} ({info_str})"
            elif label:
                return label
            elif info_str:
                return info_str
    return ""


NODE_CLASS_MAPPINGS = {"QueueLabel": QueueLabel}
NODE_DISPLAY_NAME_MAPPINGS = {"QueueLabel": "Queue Label"}
WEB_DIRECTORY = "./js"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


# ── Auto-backup (runs every 2 seconds) ───────────────────────────
_last_backup_hash = None
_backup_thread = None


def _do_auto_backup():
    global _last_backup_hash
    try:
        pq = PromptServer.instance.prompt_queue
        save_items = []
        with pq.mutex:
            for item in pq.queue:
                save_items.append({
                    "priority": _get_priority(item),
                    "prompt_id": item[1],
                    "prompt": item[2] if len(item) > 2 else {},
                    "extra_data": item[3] if len(item) > 3 else {},
                    "outputs_to_execute": item[4] if len(item) > 4 else [],
                })
            for task_id, item in pq.currently_running.items():
                save_items.append({
                    "priority": _get_priority(item),
                    "prompt_id": item[1],
                    "prompt": item[2] if len(item) > 2 else {},
                    "extra_data": item[3] if len(item) > 3 else {},
                    "outputs_to_execute": item[4] if len(item) > 4 else [],
                    "was_running": True,
                })

        current_ids = tuple(sorted(i["prompt_id"] for i in save_items))
        if current_ids == _last_backup_hash:
            return
        _last_backup_hash = current_ids

        save_data = {
            "version": 1,
            "saved_at": int(time.time() * 1000),
            "item_count": len(save_items),
            "items": save_items,
        }
        tmp_path = _AUTO_BACKUP_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(save_data, f)
        os.replace(tmp_path, _AUTO_BACKUP_PATH)
    except Exception as e:
        logging.warning(f"{LOG_PREFIX} Auto-backup failed: {e}")


def _backup_loop():
    while True:
        time.sleep(2)
        _do_auto_backup()


def _start_backup_thread():
    global _backup_thread
    if _backup_thread is None:
        _backup_thread = threading.Thread(target=_backup_loop, daemon=True)
        _backup_thread.start()
        logging.info(f"{LOG_PREFIX} Auto-backup thread started")


# ── Shared load logic ─────────────────────────────────────────────
async def _load_queue_from_file(save_path):
    import execution

    if not os.path.exists(save_path):
        return {"error": "No saved queue file found", "status": 404}

    try:
        with open(save_path, "r", encoding="utf-8") as f:
            save_data = json.load(f)
    except Exception as e:
        return {"error": f"Could not read save file: {e}", "status": 500}

    if save_data.get("version") != 1:
        return {"error": "Unknown save file version", "status": 400}

    items = save_data.get("items", [])
    if not items:
        return {"ok": True, "loaded": 0, "held": 0, "errors": []}

    pq = PromptServer.instance.prompt_queue
    loaded = 0
    held = 0
    errors = []

    for saved_item in items:
        prompt = saved_item.get("prompt", {})
        prompt_id = saved_item.get("prompt_id", str(uuid.uuid4()))
        priority = saved_item.get("priority", DEFAULT_PRIORITY)
        extra_data = saved_item.get("extra_data", {})
        outputs_to_execute = saved_item.get("outputs_to_execute", [])

        try:
            valid = await execution.validate_prompt(prompt_id, prompt, outputs_to_execute)
            is_valid = valid[0]
        except Exception as e:
            is_valid = False
            errors.append({"prompt_id": prompt_id, "error": str(e)})

        if not is_valid:
            priority = HOLD_PRIORITY
            label = _extract_label(prompt)
            errors.append({
                "prompt_id": prompt_id,
                "label": label,
                "error": "Failed validation — placed on hold",
            })
            held += 1

        new_prompt_id = str(uuid.uuid4())
        extra_data["create_time"] = int(time.time() * 1000)
        sort_key = _make_sort_key(priority)
        queue_item = (sort_key, new_prompt_id, prompt, extra_data, outputs_to_execute, {})

        with pq.mutex:
            heapq.heappush(pq.queue, queue_item)
            pq.not_empty.notify()

        loaded += 1

    pq.server.queue_updated()
    logging.info(f"{LOG_PREFIX} Loaded {loaded} items ({held} on hold)")
    return {"ok": True, "loaded": loaded, "held": held, "errors": errors}


# ── Auto-restore on startup ──────────────────────────────────────
_restore_message = None


def _schedule_auto_restore():
    global _restore_message

    if not os.path.exists(_AUTO_BACKUP_PATH):
        logging.info(f"{LOG_PREFIX} No auto-backup found")
        _start_backup_thread()
        return

    try:
        with open(_AUTO_BACKUP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("item_count", 0) == 0:
            logging.info(f"{LOG_PREFIX} Auto-backup is empty")
            _start_backup_thread()
            return
    except Exception:
        _start_backup_thread()
        return

    async def _do_restore():
        global _restore_message
        await asyncio.sleep(3)
        _install_patch()

        # Re-read file right before restoring (backup thread may have updated it)
        try:
            with open(_AUTO_BACKUP_PATH, "r", encoding="utf-8") as f:
                check = json.load(f)
            if check.get("item_count", 0) == 0:
                logging.info(f"{LOG_PREFIX} Auto-backup now empty — skipping restore")
                return
        except Exception:
            return

        # Pause, then load
        set_paused(True)
        result = await _load_queue_from_file(_AUTO_BACKUP_PATH)

        if result.get("ok") and result.get("loaded", 0) > 0:
            loaded = result.get("loaded", 0)
            held = result.get("held", 0)
            msg = f"Restored {loaded} item(s) from backup. Queue is paused."
            if held > 0:
                msg += f"\n{held} item(s) failed validation and are on hold."
            _restore_message = msg
            logging.info(f"{LOG_PREFIX} {msg}")
        else:
            # Nothing loaded — unpause
            set_paused(False)
            if not result.get("ok"):
                logging.warning(f"{LOG_PREFIX} Auto-restore failed: {result.get('error')}")
            else:
                logging.info(f"{LOG_PREFIX} No items to restore")

        # Now safe to start the backup thread
        _start_backup_thread()

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_do_restore())
        logging.info(f"{LOG_PREFIX} Auto-restore scheduled")
    except Exception as e:
        logging.warning(f"{LOG_PREFIX} Could not schedule auto-restore: {e}")


_schedule_auto_restore()


# ── API routes ────────────────────────────────────────────────────
@PromptServer.instance.routes.get("/queue_control/status")
async def qc_status(request):
    global _restore_message
    msg = _restore_message
    _restore_message = None
    resp = {"paused": is_paused(), "patch_installed": _patch_installed}
    if msg:
        resp["restore_message"] = msg
    return web.json_response(resp)


@PromptServer.instance.routes.post("/queue_control/pause")
async def qc_pause(request):
    _install_patch()
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    paused = data.get("paused")
    if paused is None:
        set_paused(not is_paused())
    else:
        set_paused(bool(paused))
    return web.json_response({"paused": is_paused(), "patch_installed": _patch_installed})


@PromptServer.instance.routes.get("/queue_control/queue")
async def qc_queue_list(request):
    _install_patch()
    pq = PromptServer.instance.prompt_queue
    with pq.mutex:
        items = []
        sorted_queue = sorted(pq.queue, key=lambda x: x[0])
        for pos, item in enumerate(sorted_queue):
            prompt = item[2] if len(item) > 2 else {}
            extra_data = item[3] if len(item) > 3 else {}
            items.append({
                "position": pos + 1,
                "prompt_id": item[1],
                "priority": _get_priority(item),
                "create_time": extra_data.get("create_time", 0),
                "label": _extract_label(prompt),
            })
        running = []
        for task_id, item in pq.currently_running.items():
            prompt = item[2] if len(item) > 2 else {}
            extra_data = item[3] if len(item) > 3 else {}
            running.append({
                "prompt_id": item[1],
                "create_time": extra_data.get("create_time", 0),
                "label": _extract_label(prompt),
            })
    return web.json_response({"paused": is_paused(), "running": running, "queued": items})


@PromptServer.instance.routes.post("/queue_control/priority")
async def qc_set_priority(request):
    _install_patch()
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    prompt_id = data.get("prompt_id")
    new_priority = data.get("priority")
    if prompt_id is None or new_priority is None:
        return web.json_response({"error": "Need prompt_id and priority"}, status=400)
    new_priority = int(new_priority)
    if new_priority < 0 or new_priority > 9:
        return web.json_response({"error": "Priority must be 0-9"}, status=400)

    pq = PromptServer.instance.prompt_queue
    with pq.mutex:
        target_idx = None
        for i, item in enumerate(pq.queue):
            if item[1] == prompt_id:
                target_idx = i
                break
        if target_idx is None:
            return web.json_response({"error": "Item not found in queue"}, status=404)
        if new_priority == 0:
            for i, item in enumerate(pq.queue):
                if _get_priority(item) == 0 and item[1] != prompt_id:
                    old_seq = _get_sequence(item)
                    pq.queue[i] = (_make_sort_key(1, old_seq),) + item[1:]
        target_item = pq.queue[target_idx]
        old_seq = _get_sequence(target_item)
        pq.queue[target_idx] = (_make_sort_key(new_priority, old_seq),) + target_item[1:]
        heapq.heapify(pq.queue)
        pq.server.queue_updated()
        pq.not_empty.notify()

    logging.info(f"{LOG_PREFIX} Set {prompt_id[:8]}... to priority {new_priority}")
    return web.json_response({"ok": True, "prompt_id": prompt_id, "priority": new_priority})


@PromptServer.instance.routes.post("/queue_control/save")
async def qc_save(request):
    _install_patch()
    try:
        data = await request.json()
    except Exception:
        data = {}
    include_running = data.get("include_running", False)
    pq = PromptServer.instance.prompt_queue
    save_items = []
    with pq.mutex:
        for item in pq.queue:
            save_items.append({
                "priority": _get_priority(item),
                "prompt_id": item[1],
                "prompt": item[2] if len(item) > 2 else {},
                "extra_data": item[3] if len(item) > 3 else {},
                "outputs_to_execute": item[4] if len(item) > 4 else [],
            })
        if include_running:
            for task_id, item in pq.currently_running.items():
                save_items.append({
                    "priority": _get_priority(item),
                    "prompt_id": item[1],
                    "prompt": item[2] if len(item) > 2 else {},
                    "extra_data": item[3] if len(item) > 3 else {},
                    "outputs_to_execute": item[4] if len(item) > 4 else [],
                    "was_running": True,
                })
    save_data = {
        "version": 1,
        "saved_at": int(time.time() * 1000),
        "item_count": len(save_items),
        "items": save_items,
    }
    save_path = os.path.join(_EXTENSION_DIR, "saved_queue.json")
    try:
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(save_data, f, indent=2)
        logging.info(f"{LOG_PREFIX} Saved {len(save_items)} items to {save_path}")
        return web.json_response({"ok": True, "count": len(save_items), "path": save_path})
    except Exception as e:
        logging.error(f"{LOG_PREFIX} Save failed: {e}")
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.post("/queue_control/load")
async def qc_load(request):
    _install_patch()
    result = await _load_queue_from_file(os.path.join(_EXTENSION_DIR, "saved_queue.json"))
    if "error" in result:
        return web.json_response(result, status=result.get("status", 500))
    return web.json_response(result)


@PromptServer.instance.routes.get("/queue_control/has_save")
async def qc_has_save(request):
    save_path = os.path.join(_EXTENSION_DIR, "saved_queue.json")
    exists = os.path.exists(save_path)
    info = {}
    if exists:
        try:
            with open(save_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            info["item_count"] = data.get("item_count", 0)
            info["saved_at"] = data.get("saved_at", 0)
        except Exception:
            pass
    return web.json_response({"exists": exists, **info})
