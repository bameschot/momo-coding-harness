use std::fmt;

pub const MAX_ITEMS: usize = 50;

#[derive(Debug, Clone)]
pub struct Item {
    pub sku: String,
    pub cents: i64,
}

pub enum Status {
    Open,
    Paid { at: u64 },
}

pub trait Priced {
    fn cents(&self) -> i64;
    fn doubled(&self) -> i64 {
        self.cents() * 2
    }
}

impl Priced for Item {
    fn cents(&self) -> i64 {
        self.cents
    }
}

impl fmt::Display for Item {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{}", self.sku)
    }
}

impl Item {
    pub fn new(sku: &str, cents: i64) -> Self {
        Item { sku: sku.to_string(), cents }
    }
}
