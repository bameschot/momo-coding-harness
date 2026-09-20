import * as React from 'react';

interface Props {
  label: string;
}

function Button({ label }: Props) {
  return <button>{label}</button>;
}

class Panel extends React.Component<Props> {
  render() {
    return <Button label="x" />;
  }
}
