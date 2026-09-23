import Card, { List, ThemeContext } from "./Widgets";

export function Page({ names }: { names: string[] }) {
  return (
    <ThemeContext.Provider value="dark">
      <Card title="Hello" />
      <List items={names} render={(n) => <b>{n}</b>} />
    </ThemeContext.Provider>
  );
}
