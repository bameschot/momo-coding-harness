/// Round cents to the nearest ten.
pub fn round_ten(cents: i64) -> i64 {
    (cents + 5) / 10 * 10
}
