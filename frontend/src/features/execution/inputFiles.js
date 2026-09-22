// Local text import only. No upload or model call happens here.
export const MAX_INPUT_FILE_BYTES = 1024 * 1024;
export async function readInputFile(file) {
  if (!file) throw new Error('Choose a file.');
  if (file.size > MAX_INPUT_FILE_BYTES) throw new Error('File exceeds 1 MB. Split the material before importing; nothing was truncated.');
  if (!/\.(txt|md|json|csv|tsv|log)$/i.test(file.name)) throw new Error('Choose a UTF-8 text, Markdown, JSON, CSV, TSV, or log file. PDF and Word need text extraction first.');
  const buffer = await file.arrayBuffer();
  let text;
  try { text = new TextDecoder('utf-8', {fatal:true}).decode(buffer); }
  catch { throw new Error('This file is not UTF-8 text. Export it as UTF-8 and try again.'); }
  if (text.includes('\0')) throw new Error('Binary content detected. Choose a text file.');
  if (!text.trim()) throw new Error('The file is empty.');
  return text;
}
