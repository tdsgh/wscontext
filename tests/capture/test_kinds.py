from wscontext.capture.kinds import ctx_invocations


def test_redirections_are_not_arguments():
    assert ctx_invocations("( ctx find a 2>&1 | head )") == [("find", ["find", "a"])]
    assert ctx_invocations("ctx show x > out.txt") == [("show", ["show", "x"])]
    assert ctx_invocations("ctx find a b 2>/dev/null") == [("find", ["find", "a", "b"])]
    assert ctx_invocations("ctx find a &>log < in.txt") == [("find", ["find", "a"])]
