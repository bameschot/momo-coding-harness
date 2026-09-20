use std::collections::HashMap;

pub struct Parser {
    pub size: usize,
}

impl Parser {
    pub fn parse(&self, text: &str) -> usize {
        text.len()
    }
}

pub fn run(p: &Parser) -> usize {
    p.parse("hi")
}
