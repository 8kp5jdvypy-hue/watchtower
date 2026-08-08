export default function PerchMark({ size = 26, className = '' }) {
  return (
    <svg
      className={`perch-mark ${className}`}
      width={size * 2.6}
      height={size}
      viewBox="0 0 260 100"
      aria-hidden="true"
    >
      <path
        className="pm-bird"
        d="M64 14 L54 10 Q52 2 42 3 Q30 4 26 14 Q20 24 6 32 Q4 34 8 34
           Q22 33 30 30 Q34 40 44 40 Q56 38 58 26 Q60 18 64 14 Z"
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
