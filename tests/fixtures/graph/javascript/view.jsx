import { useState } from "./hooks";

export const View = ({ items }) => {
  const [open, setOpen] = useState(false);
  return <ul onClick={() => setOpen(!open)}>{items.map(render)}</ul>;
};

export function render(item) {
  return <li key={item.id}>{format(item)}</li>;
}
