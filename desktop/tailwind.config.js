/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          0: "#0a0a0b",
          1: "#101013",
          2: "#16161a",
          3: "#1c1c22",
        },
        line: "#26262d",
        ink: {
          0: "#f4f4f6",
          1: "#c8c8d0",
          2: "#8a8a96",
          3: "#5b5b66",
        },
        accent: "#7cd1ff",
        ok: "#7cf2a1",
        warn: "#ffd166",
        bad: "#ff6b6b",
      },
      fontFamily: {
        mono: ["ui-monospace", "Menlo", "Monaco", "monospace"],
      },
    },
  },
  plugins: [],
};
