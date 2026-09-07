#!/usr/bin/env bash
# Fix an already-applied tree: ShyDecorator takes two arguments, not three.
#
#     error: no matching function for call to
#            'stackchan::avatar::ShyDecorator::ShyDecorator(_lv_obj_t*, int, int)'
#
# apply-face-and-touch.sh emitted the constructor shape used by Heart, Angry,
# Sweat and Dizzy -- (parent, destroyAfterMs, animationIntervalMs). Shy has no
# animation interval because it does not animate: it is two static blush
# images, left and right, where the others cycle frames.
#
# apply-face-and-touch.sh is fixed too, so a fresh apply is correct. This
# script is only for a tree where the wrong call has already landed. Safe to
# run either way -- it does nothing if the call is already correct.
set -euo pipefail

AL="${1:-$HOME/stackchan-mcp/firmware}/main/boards/stackchan/avatar_live.cc"
[ -f "$AL" ] || { echo "ERROR: $AL not found"; exit 1; }

python3 - "$AL" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()

old = 'std::make_unique<stackchan::avatar::ShyDecorator>(panel->get(), 0, 500)'
new = 'std::make_unique<stackchan::avatar::ShyDecorator>(panel->get(), 0)'

if new in t and old not in t:
    print("avatar_live.cc: already correct")
elif old not in t:
    sys.exit("ABORTING -- neither the wrong nor the right call found; was the "
             "shy decorator applied at all?")
else:
    t = t.replace(old, new)
    # Keep the comment honest about why there are only two arguments.
    t = t.replace(
        '''    // reaction, so we own its lifetime. Avatar::update() animates it through
    // the board's 30 fps tick.''',
        '''    // reaction, so we own its lifetime.
    //
    // Two arguments, not three: ShyDecorator takes no animationIntervalMs
    // because it does not animate -- it is two static blush images, left and
    // right, unlike Heart/Angry/Sweat/Dizzy which cycle frames.''')
    p.write_text(t)
    print("avatar_live.cc: ShyDecorator call corrected to two arguments")
PYEOF
