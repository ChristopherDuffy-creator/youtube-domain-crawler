from pathlib import Path

path = Path("app/link_hunter_preview.py")
text = path.read_text(encoding="utf-8")
old = '''    provisional_pool = list(dict.fromkeys([*cached_targets, *targets]))
    provisional_pool.sort(
        key=lambda target: (-provisional_scores.get(target, 0.0), target)
    )
'''
new = '''    provisional_pool = list(dict.fromkeys([*cached_targets, *targets]))
    provisional_position = {target: index for index, target in enumerate(provisional_pool)}
    provisional_pool.sort(
        key=lambda target: (
            -provisional_scores.get(target, 0.0),
            provisional_position[target],
        )
    )
'''
if text.count(old) != 1:
    raise RuntimeError("expected one provisional winner-pool sort block")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("winner queue preview tie-break fixed")
