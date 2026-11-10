import { useState } from "./hooks";

export const View = ({ items }: Props) => {
  const [open, setOpen] = useState<boolean>(false);
  return <ul onClick={() => setOpen(!open)}>{items.map(render)}</ul>;
};

export function render(item: Item): JSX.Element {
  return <li key={item.id}>{format(item)}</li>;
}
