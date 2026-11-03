#!/bin/sh
# wscontext Git hook shim (@HOOK@): runs @HOOK@.pre-wscontext if present, then wscontext; exits with the original hook's code.
h="$0.pre-wscontext"; r=0; if [ -x "$h" ]; then "$h" "$@"; r=$?; fi; l=@LAUNCHER@; if [ -x "$l" ]; then "$l" git-hook @HOOK@ "$@" </dev/null >/dev/null 2>&1; fi; exit $r
