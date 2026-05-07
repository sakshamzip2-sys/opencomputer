import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function Markdown({ text }: { text: string }) {
  return (
    <article className="prose prose-invert max-w-none prose-headings:mt-6 prose-headings:mb-3 prose-h1:text-2xl prose-h2:text-xl prose-h3:text-lg prose-p:my-3 prose-li:my-0 prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800 prose-code:text-cyan-300 prose-a:text-cyan-400">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </article>
  );
}
