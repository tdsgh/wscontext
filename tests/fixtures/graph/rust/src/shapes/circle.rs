use super::Circle;
use super::super::util;
use self::detail::*;

pub fn area(radius: f64) -> f64 {
    std::f64::consts::PI * radius * radius
}
