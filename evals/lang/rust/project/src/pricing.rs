use crate::model::{Item, Priced, MAX_ITEMS};
use crate::util::round_ten;

pub static TAX_RATE: f64 = 0.21;

/// Take a percentage off an amount of cents.
pub fn apply_discount(cents: i64, pct: i64) -> i64 {
    cents - cents * pct / 100
}

pub fn total<T: Priced>(items: &[T]) -> i64 {
    let sum: i64 = items.iter().take(MAX_ITEMS).map(|i| i.cents()).sum();
    round_ten(apply_discount(sum, 10))
}

pub fn receipt(items: &[Item]) -> String {
    format!("{} items: {}", items.len(), total(items))
}
