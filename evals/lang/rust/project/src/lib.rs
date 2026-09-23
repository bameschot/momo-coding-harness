pub mod model;
pub mod pricing;
pub mod util;

pub use pricing::{apply_discount, TAX_RATE};

macro_rules! cents {
    ($e:expr) => {
        ($e * 100.0) as i64
    };
}
pub(crate) use cents;
