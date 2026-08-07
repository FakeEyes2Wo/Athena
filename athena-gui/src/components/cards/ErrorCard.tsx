interface ErrorCardProps {
  content: string;
}

/** Displays an error message in a card format. */
export function ErrorCard({ content }: ErrorCardProps) {
  return (
    <section className="card card--error">
      <h3 className="card__title">错误</h3>
      <p className="card__body">{content}</p>
    </section>
  );
}
