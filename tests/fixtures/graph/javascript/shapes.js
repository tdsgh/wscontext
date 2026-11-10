function helper(value = normalize("x")) {
  return value.trim();
}

function* ids() {
  yield next();
}

const area = async (w, h) => {
  const inner = function () {
    return helper("a");
  };
  return w * h + inner();
};

const short = (n) => double(n);

let a = () => 1, b = () => a();

export class Shape extends Base {
  static count = make();
  handler = (e) => this.onEvent(e);

  constructor(name) {
    super();
    registry.add(name);
  }

  area() {
    return helper(this.name).length;
  }

  get label() {
    return this.name.toUpperCase();
  }

  static create() {
    return new Shape("s");
  }

  #secret() {
    return new Point3D();
  }

  [Symbol.iterator]() {}
}

module.exports = { helper, area };

register(Shape);
