"""Definitions and calls: functions, classes, methods, nesting, decorators."""

import functools


def helper(x):
    return normalize(x)


@functools.cache
def cached(key, fallback=helper(0)):
    def inner(y):
        return helper(y)

    return inner(key)


class Shape(Base, metaclass=Meta):
    registry = make_registry()

    def __init__(self, name):
        self.name = name.strip()

    @property
    def area(self):
        return compute(self).value()

    async def fetch(
        self,
        url: str,
    ) -> bytes:
        return await client.get(url)

    class Inner:
        def method(self):
            return Shape("inner")


@register("square")
class Square(Shape):
    def area(self):
        return super().area * 2


main(helper(1))
