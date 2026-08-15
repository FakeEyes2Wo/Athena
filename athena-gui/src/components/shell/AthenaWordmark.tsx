/** 顶栏 Athena 品牌字标：SVG 渐变 + Geist 900 特粗，厚重且有品牌识别度。 */
export function AthenaWordmark() {
  return (
    <svg height="28" viewBox="0 0 126 30" role="img" aria-label="Athena" fill="none">
      <defs>
        <linearGradient id="athena-wordmark-gradient" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" style={{ stopColor: "var(--accent)" }} />
          <stop offset="1" style={{ stopColor: "var(--chart-4)" }} />
        </linearGradient>
      </defs>
      <text
        x="0"
        y="24.5"
        fontFamily="Geist, system-ui, -apple-system, 'Segoe UI', sans-serif"
        fontWeight="900"
        fontSize="26"
        letterSpacing="-0.6"
        fill="url(#athena-wordmark-gradient)"
      >
        Athena
      </text>
    </svg>
  );
}
