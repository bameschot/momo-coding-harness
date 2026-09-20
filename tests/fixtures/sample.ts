import { helper } from './helper';

interface Runner {
  run(): number;
}

class Parser implements Runner {
  parse(text: string): number {
    return text.length;
  }
  run(): number {
    return this.parse('hi');
  }
}

type Id = string;

function run(p: Parser): number {
  return p.parse('hi');
}
