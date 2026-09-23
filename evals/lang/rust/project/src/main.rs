use shop::model::Item;
use shop::pricing::receipt;

fn main() {
    let items = vec![Item::new("tea", 1200)];
    println!("{}", receipt(&items));
}
