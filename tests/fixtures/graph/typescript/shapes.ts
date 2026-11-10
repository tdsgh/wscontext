import { registry } from "./registry";

export interface Drawable<T> extends Base {
  draw(target: T): void;
}

export type Point = { x: number; y: number };

enum Color {
  Red = paint("red"),
  Blue,
}

export function helper(value: string = normalize("x")): string {
  return value.trim();
}

export const area = async (w: number, h: number): Promise<number> => {
  const inner = function () {
    return helper("a");
  };
  return w * h + inner();
};

const short = (n: number) => double(n);

const a = () => 1, b = () => a();

@sealed
export abstract class Shape<T> extends Base implements Drawable<T> {
  static count = make();
  handler = (e: Event) => this.onEvent(e);

  constructor(private name: string) {
    super();
    registry.add(name);
  }

  @memo()
  @log
  area(): number {
    return helper(this.name).length;
  }

  abstract perimeter(): number;

  get label(): string {
    return this.name.toUpperCase();
  }

  #secret() {
    return new Point3D();
  }

  [Symbol.iterator]() {}
}

export default class extends Shape<number> {}

namespace Geometry {
  export function unit(): Shape<number> {
    return new Shape<number>("unit");
  }
}

function over(a: string): void;
function over(a: number): void;
function over(a: any): void {
  a?.toString();
}

register(Shape);
