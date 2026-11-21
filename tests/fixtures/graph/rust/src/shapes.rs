//! Shapes, with their children in `shapes/`.

pub mod circle;

use crate::util::helper;
use super::Foo;

pub struct Circle {
    pub radius: f64,
}

impl crate::Area for Circle {
    type Unit = f64;
    const SIDES: u32 = 0;

    fn area(&self) -> f64 {
        circle::area(self.radius)
    }
}

pub enum Kind {
    Round,
    Square(u8),
}
