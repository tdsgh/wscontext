#!/bin/sh
# wscontext Git hook shim (@HOOK@): runs @HOOK@.pre-wscontext if present, then wscontext, both on the hook input; exits with the original hook's code.
i=$(cat); h="$0.pre-wscontext"; r=0; if [ -x "$h" ]; then printf "%s${i:+\n}" "$i" | "$h" "$@"; r=$?; fi; l=@LAUNCHER@; if @WHEN@[ -x "$l" ]; then printf "%s${i:+\n}" "$i" | "$l" git-hook @HOOK@ "$@" >/dev/null 2>&1; fi; exit $r
