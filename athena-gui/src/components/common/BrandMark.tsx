interface BrandMarkProps {
  size?: number;
  /** Show the "Athena" word next to the mark. */
  withName?: boolean;
  className?: string;
}

/** Athena logo: a pixel-art cat; the light/dark variant follows the active theme. */
export function BrandMark({ size = 24, withName = false, className }: BrandMarkProps) {
  const imageStyle = { width: size, height: size, borderRadius: Math.round(size * 0.28) };
  return (
    <span
      className={className}
      style={{ display: "inline-flex", alignItems: "center", gap: 9 }}
    >
      <img
        className="brand-logo brand-logo--light"
        src="/athena-logo-light.png"
        alt=""
        aria-hidden
        draggable={false}
        style={imageStyle}
      />
      <img
        className="brand-logo brand-logo--dark"
        src="/athena-logo-dark.png"
        alt=""
        aria-hidden
        draggable={false}
        style={imageStyle}
      />
      {withName && (
        <span
          style={{
            fontSize: Math.round(size * 0.66),
            fontWeight: 600,
            letterSpacing: "-0.015em",
            color: "var(--text-primary)",
          }}
        >
          Athena
        </span>
      )}
    </span>
  );
}
