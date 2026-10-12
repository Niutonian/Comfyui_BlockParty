"""Allow older H3 keyframe guards to recognize verified native core support.

Only the node's capability guard is adapted. PackedLayout, keyframe positions,
the pinned node repository, and workflow inputs remain intact.
"""
import functools
import sys


@functools.lru_cache(maxsize=8)
def _probe_native(module, constructor):
    import torch
    try:
        # The user's first/middle/last anchors, on small CPU-only latents.
        keyframes = [{'resolved_frame_index': index,
                      'latent': torch.zeros(1, 24, 1, 4, 4, device='cpu')}
                     for index in (0, 121, 242)]
        layout = module.PackedLayout(11, 72, 4, 4, 8, keyframes=keyframes)
        positions = []
        for start, end, kind in layout.segments:
            if kind == 'cond': positions.append((start, end))
        if len(positions) != 3: return False
        for index, (start, end) in zip((0, 121, 242), positions):
            expected = 11 + module.FRAME_RESCALE * index
            rows = layout.position_ids[start:end, 0]
            if end <= start or not torch.allclose(rows, torch.full_like(rows, expected), atol=1e-9, rtol=0):
                return False
        return True
    except Exception:
        return False


def native_interior_supported():
    try:
        import comfy.ldm.minimax.model as module
        return _probe_native(module, module.PackedLayout.__init__)
    except (ImportError, AttributeError):
        return False


def install_native_guard(registry):
    cls = registry.get('H3Keyframes')
    module = sys.modules.get(getattr(cls, '__module__', ''))
    guard = getattr(module, 'ensure_interior_keyframes', None)
    if not callable(guard) or getattr(guard, '_block_party_native_h3_guard', False) is True: return False
    if not native_interior_supported(): return False

    @functools.wraps(guard)
    def native_guard(*args, **kwargs):
        # A different extension may replace the layout after this hook. Verify
        # that constructor too; never mask a later incompatible patch.
        if native_interior_supported():
            return True, 'Block Party verified native first, middle, and last keyframe positions; no layout patch needed'
        return guard(*args, **kwargs)

    native_guard._block_party_native_h3_guard = True
    module.ensure_interior_keyframes = native_guard
    return True


def on_prompt(payload):
    graph = payload.get('prompt', {}) if isinstance(payload, dict) else {}
    if isinstance(graph, dict) and any(isinstance(node, dict) and node.get('class_type') == 'H3Keyframes'
                                       for node in graph.values()):
        import nodes
        install_native_guard(nodes.NODE_CLASS_MAPPINGS)
    return payload
