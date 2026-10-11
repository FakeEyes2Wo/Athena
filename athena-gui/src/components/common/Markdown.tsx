import { memo } from "react";
import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// GFM: agent reports lean on tables (metrics, confusion matrices), task lists and strikethrough.
const REMARK_PLUGINS = [remarkGfm];

const COMPONENTS: Components = {
  // Links leave the app window instead of navigating it away.
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" />,
};

/** Keep react-markdown's safe defaults, but allow inline images the backend embedded. */
function allowDataImages(url: string, key: string): string {
  if (key === "src" && /^data:image\/(png|jpe?g|gif|webp|svg\+xml);base64,/i.test(url)) {
    return url;
  }
  return defaultUrlTransform(url);
}

interface MarkdownProps {
  children: string;
  className?: string;
  /** Render ``data:image/...;base64`` sources (e.g. EDA figures inlined by the backend). */
  dataImages?: boolean;
}

/** Markdown prose with the shared ``.markdown`` typography. Raw HTML is not rendered. */
export const Markdown = memo(function Markdown({ children, className, dataImages }: MarkdownProps) {
  return (
    <div className={className ? `markdown ${className}` : "markdown"}>
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        components={COMPONENTS}
        urlTransform={dataImages ? allowDataImages : defaultUrlTransform}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
});
