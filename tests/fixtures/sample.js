import { helper } from './helper.js';

class Parser {
  parse(text) {
    return text.length;
  }
}

function run(p) {
  return p.parse('hi');
}

const shout = (s) => s.toUpperCase();
