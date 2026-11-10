import def from "./default";
import * as ns from "./ns";
import { a, b as c } from "./named";
import type { T } from "./types";
import fs = require("fs");
import "./side-effect";
export * from "./all";
export * as tools from "./tools";
export { x, y as z } from "./reexport";
export { local };
const m = require("./cjs");
const { q } = require("./destructured");
const dynamic = require(name);

export async function lazy() {
  const mod = await import("./lazy");
  return import(path);
}
