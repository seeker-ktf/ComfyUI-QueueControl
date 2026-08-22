"""
ComfyUI-QueueControl — Pause/unpause + priority system for the prompt queue.
Priority 0-8: normal (lower runs first). 9: on hold (never runs).
Only one priority-0 item allowed; setting a new 0 bumps the old one to 1.
New jobs enter at priority 5.
"""

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
    """Build the numeric sort key: priority * multiplier + sequence."""
    if seq is None:
        seq = _next_sequence()
    return priority * PRIORITY_MULTIPLIER + seq


def _get_priority(item):
    """Extract priority (0-9) from a queue item's sort key."""
    return int(item[0] // PRIORITY_MULTIPLIER)


def _get_sequence(item):
    """Extract sequence number from a queue item's sort key."""
    return int(item[0] % PRIORITY_MULTIPLIER)


# ── Queue patches (deferred until first use) ─────────────────────
_patch_installed = False


def _install_patch():
    global _patch_installed
    if _patch_installed:
        return True
    try:
        pq = PromptServer.instance.prompt_queue

        # Patch get() — respect pause flag and skip priority-9 items
        def _patched_get(self, timeout=None):
            with self.not_empty:
                while True:
                    # Wait while paused or empty
                    if is_paused() or len(self.queue) == 0:
                        self.not_empty.wait(timeout=timeout)
                        if timeout is not None and (is_paused() or len(self.queue) == 0):
                            return None
                        continue
                    # Check if top item is on hold (priority 9)
                    top = self.queue[0]
                    if _get_priority(top) >= HOLD_PRIORITY:
                        # Everything remaining is on hold — treat as empty
                        self.not_empty.wait(timeout=timeout)
                        if timeout is not None:
                            return None
                        continue
                    # Good to go — pop it
                    item = heapq.heappop(self.queue)
                    i = self.task_counter
                    self.currently_running[i] = copy.deepcopy(item)
                    self.task_counter += 1
                    self.server.queue_updated()
                    return (item, i)

        # Patch put() — override sort key with priority 5
        _original_put = pq.put

        def _patched_put(self, item):
            # Replace the number (index 0) with our priority sort key
            new_item = (_make_sort_key(DEFAULT_PRIORITY),) + item[1:]
            with self.mutex:
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


# ── Queue Label Node ──────────────────────────────────────────────
class QueueLabel:
    """A node that holds a display name for this workflow in the queue panel."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "label": ("STRING", {"default": "", "multiline": False}),
            },
            "optional": {
                "info": ("*",),
            },
        }

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "execute"
    CATEGORY = "QueueControl"

    def execute(self, label, info=None):
        # Does nothing at runtime — the label is read from the prompt data
        return {}


def _trace_value(prompt, ref, depth=0):
    """Follow a connection reference through the prompt to find a literal value.
    refs look like ["node_id", output_index]. We look up that node's inputs
    and try to find a literal. Gives up after 10 hops to avoid loops."""
    if depth > 10:
        return None
    if not isinstance(ref, list) or len(ref) < 2:
        return None
    node_id = str(ref[0])
    node_data = prompt.get(node_id)
    if not isinstance(node_data, dict):
        return None
    inputs = node_data.get("inputs", {})
    # Look through inputs for a literal value we can use
    for key, val in inputs.items():
        if isinstance(val, list):
            # It's another connection — trace it
            result = _trace_value(prompt, val, depth + 1)
            if result is not None:
                return result
        elif isinstance(val, (str, int, float)):
            # Found a literal — return it if it looks meaningful
            if isinstance(val, str) and val.strip():
                return val.strip()
            elif isinstance(val, (int, float)):
                return str(val)
    return None


def _extract_label(prompt):
    """Scan a prompt dict for a QueueLabel node and return its label text."""
    if not isinstance(prompt, dict):
        return ""
    for node_id, node_data in prompt.items():
        if isinstance(node_data, dict) and node_data.get("class_type") == "QueueLabel":
            inputs = node_data.get("inputs", {})
            label = inputs.get("label", "")
            info = inputs.get("info", None)

            # Resolve info if it's a connection
            info_str = ""
            if isinstance(info, list):
                resolved = _trace_value(prompt, info)
                if resolved:
                    info_str = resolved
            elif isinstance(info, (str, int, float)):
                info_str = str(info).strip()

            # Combine label and info
            if label and info_str:
                return f"{label} ({info_str})"
            elif label:
                return label
            elif info_str:
                return info_str
    return ""


NODE_CLASS_MAPPINGS = {
    "QueueLabel": QueueLabel,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "QueueLabel": "Queue Label",
}
WEB_DIRECTORY = "./js"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


# ── API routes ────────────────────────────────────────────────────
@PromptServer.instance.routes.get("/queue_control/status")
async def qc_status(request):
    return web.json_response({
        "paused": is_paused(),
        "patch_installed": _patch_installed,
    })


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
    """List all queued items with priority and submission info."""
    _install_patch()
    pq = PromptServer.instance.prompt_queue
    with pq.mutex:
        items = []
        # Sort a copy for display (heapq order)
        sorted_queue = sorted(pq.queue, key=lambda x: x[0])
        for pos, item in enumerate(sorted_queue):
            priority = _get_priority(item)
            prompt_id = item[1]
            prompt = item[2] if len(item) > 2 else {}
            extra_data = item[3] if len(item) > 3 else {}
            create_time = extra_data.get("create_time", 0)
            label = _extract_label(prompt)
            items.append({
                "position": pos + 1,
                "prompt_id": prompt_id,
                "priority": priority,
                "create_time": create_time,
                "label": label,
            })

        # Also include currently running
        running = []
        for task_id, item in pq.currently_running.items():
            prompt_id = item[1]
            prompt = item[2] if len(item) > 2 else {}
            extra_data = item[3] if len(item) > 3 else {}
            create_time = extra_data.get("create_time", 0)
            label = _extract_label(prompt)
            running.append({
                "prompt_id": prompt_id,
                "create_time": create_time,
                "label": label,
            })

    return web.json_response({
        "paused": is_paused(),
        "running": running,
        "queued": items,
    })


@PromptServer.instance.routes.post("/queue_control/priority")
async def qc_set_priority(request):
    """Change an item's priority. Enforces only-one-zero rule."""
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
        # Find the target item
        target_idx = None
        for i, item in enumerate(pq.queue):
            if item[1] == prompt_id:
                target_idx = i
                break

        if target_idx is None:
            return web.json_response({"error": "Item not found in queue"}, status=404)

        # If setting to 0, bump any existing 0 to 1
        if new_priority == 0:
            for i, item in enumerate(pq.queue):
                if _get_priority(item) == 0 and item[1] != prompt_id:
                    old_seq = _get_sequence(item)
                    pq.queue[i] = (_make_sort_key(1, old_seq),) + item[1:]

        # Update the target item's priority (keep its sequence for FIFO)
        target_item = pq.queue[target_idx]
        old_seq = _get_sequence(target_item)
        pq.queue[target_idx] = (_make_sort_key(new_priority, old_seq),) + target_item[1:]

        heapq.heapify(pq.queue)
        pq.server.queue_updated()

        # Wake the queue thread in case something became runnable
        pq.not_empty.notify()

    logging.info(f"{LOG_PREFIX} Set {prompt_id[:8]}... to priority {new_priority}")
    return web.json_response({"ok": True, "prompt_id": prompt_id, "priority": new_priority})

@PromptServer.instance.routes.post("/queue_control/save")
async def qc_save(request):
    """Save the current queue to a JSON file."""
    _install_patch()
    try:
        data = await request.json()
    except Exception:
        data = {}

    include_running = data.get("include_running", False)
    pq = PromptServer.instance.prompt_queue
    save_items = []

    with pq.mutex:
        # Save queued items
        for item in pq.queue:
            priority = _get_priority(item)
            save_items.append({
                "priority": priority,
                "prompt_id": item[1],
                "prompt": item[2] if len(item) > 2 else {},
                "extra_data": item[3] if len(item) > 3 else {},
                "outputs_to_execute": item[4] if len(item) > 4 else [],
                # Skip index 5 (sensitive data)
            })

        # Optionally save running items
        if include_running:
            for task_id, item in pq.currently_running.items():
                priority = _get_priority(item)
                save_items.append({
                    "priority": priority,
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
    """Load a saved queue from disk and push items back into the queue."""
    _install_patch()
    import execution

    save_path = os.path.join(_EXTENSION_DIR, "saved_queue.json")
    if not os.path.exists(save_path):
        return web.json_response({"error": "No saved queue file found"}, status=404)

    try:
        with open(save_path, "r", encoding="utf-8") as f:
            save_data = json.load(f)
    except Exception as e:
        return web.json_response({"error": f"Could not read save file: {e}"}, status=500)

    if save_data.get("version") != 1:
        return web.json_response({"error": "Unknown save file version"}, status=400)

    items = save_data.get("items", [])
    if not items:
        return web.json_response({"ok": True, "loaded": 0, "held": 0, "errors": []})

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

        # Validate the prompt
        try:
            valid = await execution.validate_prompt(prompt_id, prompt, outputs_to_execute)
            is_valid = valid[0]
        except Exception as e:
            is_valid = False
            errors.append({"prompt_id": prompt_id, "error": str(e)})

        if not is_valid:
            # Load it anyway but put on hold
            priority = HOLD_PRIORITY
            label = _extract_label(prompt)
            errors.append({
                "prompt_id": prompt_id,
                "label": label,
                "error": "Failed validation — placed on hold",
            })
            held += 1

        # Generate new prompt_id to avoid collisions
        new_prompt_id = str(uuid.uuid4())
        # Update create_time so it sorts properly in time view
        extra_data["create_time"] = int(time.time() * 1000)

        # Build the queue tuple and push it
        sort_key = _make_sort_key(priority)
        queue_item = (sort_key, new_prompt_id, prompt, extra_data, outputs_to_execute, {})

        with pq.mutex:
            heapq.heappush(pq.queue, queue_item)
            pq.not_empty.notify()

        loaded += 1

    # Notify the UI
    pq.server.queue_updated()

    logging.info(f"{LOG_PREFIX} Loaded {loaded} items ({held} on hold)")
    return web.json_response({
        "ok": True,
        "loaded": loaded,
        "held": held,
        "errors": errors,
    })


@PromptServer.instance.routes.get("/queue_control/has_save")
async def qc_has_save(request):
    """Check if a saved queue file exists."""
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
