export interface Entity {
  id: string;
}

export type Id<T extends Entity> = T["id"];

export const enum Level {
  Low = 1,
  High = 2,
}

function logged(target: unknown, key: string): void {}

export abstract class Repo<T extends Entity> {
  protected items: T[] = [];

  abstract find(id: string): T | undefined;

  @logged
  save(item: T): T {
    this.items.push(item);
    return item;
  }

  count(): number;
  count(filter: (t: T) => boolean): number;
  count(filter?: (t: T) => boolean): number {
    return filter ? this.items.filter(filter).length : this.items.length;
  }
}

export function isEntity(x: unknown): x is Entity {
  return typeof x === "object" && x !== null && "id" in x;
}

declare module "./repo" {
  interface Entity {
    createdAt?: Date;
  }
}
