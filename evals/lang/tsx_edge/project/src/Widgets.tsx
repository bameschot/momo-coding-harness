import React, { createContext, forwardRef, memo, useContext } from "react";

export const ThemeContext = createContext("light");

export const Input = forwardRef<HTMLInputElement, { label: string }>((props, ref) => (
  <label>
    {props.label}
    <input ref={ref} />
  </label>
));

type ListProps<T> = { items: T[]; render: (t: T) => React.ReactNode };

export function List<T>({ items, render }: ListProps<T>) {
  return <ul>{items.map((t, i) => <li key={i}>{render(t)}</li>)}</ul>;
}

const Badge: React.FC<{ text: string }> = ({ text }) => {
  const theme = useContext(ThemeContext);
  return <span className={theme}>{text}</span>;
};

function Card({ title }: { title: string }) {
  return (
    <>
      <Badge text={title} />
      <Input label="name" />
    </>
  );
}

export default memo(Card);
