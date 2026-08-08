export default function PerchMark({ size = 26, className = '' }) {
  return (
    <svg
      className={`perch-mark ${className}`}
      width={size * 2.6}
      height={size}
      viewBox="0 0 260 100"
      aria-hidden="true"
    >
      {/* Perched variant of the same silhouette used in the hero
          (kestrelTexture.js) and the mid-page dive (MarketField.jsx) --
          wings folded rather than spread, standing on the branch/line
          rather than hovering, but the same rounded head + hooked beak
          language. */}
      <path
        className="pm-bird"
        d="M8 34 C4 22 12 8 28 4 C36 2 44 4 48 10 C56 8 63 12 58 18
           C54 20 48 18 44 22 C47 30 44 38 34 41 C24 43 14 40 8 34 Z"
        transform="translate(10,6) scale(1.1)"
      />
      <g className="pm-legs" transform="translate(10,6) scale(1.1)" strokeLinecap="round">
        <path d="M38 39 V50" /><path d="M46 39 V50" />
      </g>
      <path
        className="pm-line"
        d="M4 64 L30 66 L52 61 L78 63 L100 54 L125 57 L150 46 L175 49 L200 38 L225 41 L260 28"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle className="pm-tip" cx="260" cy="28" r="4" />
    </svg>
  )
}
