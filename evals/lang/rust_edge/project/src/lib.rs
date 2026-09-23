use std::fmt::Display;
use std::str::FromStr;

pub mod inner {
    pub fn helper() -> u32 {
        7
    }
}

pub struct Wrapper<T> {
    pub value: T,
}

impl<T: Display> Wrapper<T> {
    pub const LABEL: &'static str = "wrapped";

    pub fn show(&self) -> String {
        format!("{}: {}", Self::LABEL, self.value)
    }
}

pub enum Shape {
    Circle(f64),
    Square(f64),
}

impl Shape {
    pub fn area(&self) -> f64 {
        match self {
            Shape::Circle(r) => 3.14 * r * r,
            Shape::Square(s) => s * s,
        }
    }
}

pub trait Named {
    fn name(&self) -> String;
}

impl FromStr for Shape {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "circle" => Ok(Shape::Circle(1.0)),
            _ => Err(format!("unknown {}", s)),
        }
    }
}

pub(crate) async fn load(name: &str) -> Option<Shape> {
    name.parse::<Shape>().ok()
}

pub fn total(shapes: &[Shape], named: &dyn Named) -> f64 {
    let double = |x: f64| x * 2.0;
    let _ = named.name();
    shapes.iter().map(|s| double(s.area())).sum::<f64>() + inner::helper() as f64
}
