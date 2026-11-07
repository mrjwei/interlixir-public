import type { PostPolicy } from './types'

// Mirrors services/api/app/routes/uploads.py. The server and S3 enforce these too; checking here only
// saves a round trip and lets the page explain the problem next to the file.
export const MiB = 1024 * 1024
export const RECORDING_EXTENSIONS = ['mp3', 'm4a', 'wav', 'webm', 'mp4']
export const CONTEXT_EXTENSIONS = ['pdf', 'docx', 'md', 'txt']
export const MAX_RECORDINGS = 5
export const MAX_RECORDING_BYTES = 200 * MiB
export const MAX_CONTEXT_FILES = 5
export const MAX_CONTEXT_TOTAL_BYTES = 20 * MiB
export const MAX_INSTRUCTION_CHARS = 4000

export function extension(fileName: string) {
  const dot = fileName.lastIndexOf('.')
  return dot > 0 ? fileName.slice(dot + 1).toLowerCase() : ''
}

/** Why a recording can't be uploaded, or null if it can. */
export function recordingProblem(file: File): string | null {
  const ext = extension(file.name)
  if (!RECORDING_EXTENSIONS.includes(ext)) return ext ? `Unsupported format: .${ext}` : 'Unsupported format'
  if (file.size === 0) return 'File is empty'
  if (file.size > MAX_RECORDING_BYTES) return 'File is larger than 200 MB'
  return null
}

export function contextProblem(file: File): string | null {
  const ext = extension(file.name)
  if (!CONTEXT_EXTENSIONS.includes(ext)) return ext ? `Unsupported format: .${ext}` : 'Unsupported format'
  if (file.size === 0) return 'File is empty'
  return null
}

/**
 * POST one file to S3 with its pre-signed policy: every policy field first, the file last. XHR rather
 * than fetch because fetch has no upload progress. The policy's Content-Type is in `fields`, so S3 gets
 * exactly the type it was signed for.
 */
export function postToS3(
  policy: PostPolicy,
  file: File,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    for (const [name, value] of Object.entries(policy.fields)) form.append(name, value)
    form.append('file', file)

    const xhr = new XMLHttpRequest()
    xhr.open('POST', policy.url)
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1)
        resolve()
      } else {
        // S3 answers with XML: <Code>EntityTooLarge</Code>, <Code>AccessDenied</Code> (expired policy)...
        const code = /<Code>([^<]+)<\/Code>/.exec(xhr.responseText)?.[1]
        reject(new Error(code ? `Upload rejected (${code})` : `Upload failed (${xhr.status})`))
      }
    }
    xhr.onerror = () => reject(new Error('Upload failed. Check your connection.'))
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'))
    signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(form)
  })
}
