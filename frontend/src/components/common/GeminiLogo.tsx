/** The Gemini sparkle, inline with a model name in the cost projection tables. */
export function GeminiLogo() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 28 28"
      fill="none"
      style={{ display: "inline", verticalAlign: "middle", marginRight: 5, flexShrink: 0 }}
    >
      <path
        d="M14 2C14 8.627 8.627 14 2 14C8.627 14 14 19.373 14 26C14 19.373 19.373 14 26 14C19.373 14 14 8.627 14 2Z"
        fill="url(#ggl)"
      />
      <defs>
        <linearGradient id="ggl" x1="2" y1="2" x2="26" y2="26" gradientUnits="userSpaceOnUse">
          <stop stopColor="#4285F4" />
          <stop offset="1" stopColor="#8AB4F8" />
        </linearGradient>
      </defs>
    </svg>
  );
}
