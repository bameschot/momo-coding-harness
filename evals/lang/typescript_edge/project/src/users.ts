import { Repo, Entity, isEntity } from "./repo";
import type { Id } from "./repo";

export interface User extends Entity {
  name: string;
}

export default class UserRepo extends Repo<User> {
  find(id: string): User | undefined {
    return this.items.find((u) => u.id === id);
  }
}

export function load(repo?: UserRepo): number {
  const first: Id<User> = "1";
  const found = repo?.find(first);
  return isEntity(found) ? repo!.count() : 0;
}
