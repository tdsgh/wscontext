//! The fixture crate.

mod shapes;
mod util;
#[path = "elsewhere/renamed.rs"]
mod renamed;

use crate::shapes::{Circle, circle::{self, area as circle_area}, *};
use self::util::helper;
use std::fmt::{self, Debug, Display};
use serde::Serialize;
use sibling_crate::Thing;
extern crate alloc;

/// A shape with a size.
#[derive(Debug, Clone)]
pub struct Foo<T> {
    size: T,
}

// a plain comment ends the run
#[inline]
pub fn make<T: Clone>(size: T) -> Foo<T>
where
    T: Copy,
{
    let build = |s| Foo::new(s);
    build(size)
}

impl<T> Foo<T> {
    pub fn new(size: T) -> Self {
        Foo { size }
    }
}

impl<T: Debug> Display for &Foo<T> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{:?}", self.size)
    }
}

impl Debug for Foo<u8> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str(&helper(self.size))
    }
}

pub trait Area {
    type Unit;
    const SIDES: u32;

    fn area(&self) -> f64;

    fn double(&self) -> f64 {
        self.area() * 2.0
    }
}

/** Raw bits. */
pub union Bits {
    int: u32,
    float: f32,
}

pub type Pair<T> = (T, T);

pub const ZERO: u8 = 0;
static NAME: &str = "fixture";

macro_rules! square {
    ($x:expr) => {
        util::helper($x) * $x
    };
}

#[cfg(any(unix, windows))]
pub fn total(items: &[u8]) -> u32 {
    fn add(a: u32, b: &u8) -> u32 {
        a + square!(*b as u32)
    }
    let n = parse::<u32>("1");
    let v = vec![make(1), Foo::new(n)];
    items.iter().fold(0, add)
}

pub mod geometry {
    mod inner;

    use super::Foo;
    use self::inner::Point;
    use super::super::outside;

    pub struct Line;

    impl Line {
        pub fn length(&self) -> f64 {
            inner::distance()
        }
    }

    pub mod nested {
        use super::super::make;
        use super::Line;

        pub fn deep() {}
    }
}

#[cfg( test )]
mod tests {
    use super::*;

    fn fixture() -> Foo<u8> {
        make(3)
    }

    #[test]
    fn makes_a_foo() {
        assert_eq!(fixture().size, total(&[1]));
    }

    #[tokio::test]
    async fn runs_async() {
        assert!(check(make(2)));
    }
}
