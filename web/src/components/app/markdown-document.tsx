import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** The synthesised document. react-markdown renders no raw HTML, so model output can't inject markup. */
export default function MarkdownDocument({ markdown }: { markdown: string }) {
  return (
    <article className="prose-doc mx-auto">
      <Markdown remarkPlugins={[remarkGfm]}>{markdown}</Markdown>
    </article>
  )
}
