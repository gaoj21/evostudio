import { expect, it } from 'vitest';
import { readInputFile, MAX_INPUT_FILE_BYTES } from './inputFiles.js';
const file=(name,text)=>({name,size:text.length,arrayBuffer:async()=>new TextEncoder().encode(text).buffer});
it('preserves text for review without executing or truncating',async()=>{
 expect(await readInputFile(file('notes.md','# Notes\nCustomer feedback'))).toBe('# Notes\nCustomer feedback');
});
it('rejects unsupported, oversized and binary files',async()=>{
 await expect(readInputFile(file('paper.pdf','data'))).rejects.toThrow('PDF');
 await expect(readInputFile({...file('notes.txt','x'),size:MAX_INPUT_FILE_BYTES+1})).rejects.toThrow('1 MB');
 await expect(readInputFile(file('notes.txt','a\0b'))).rejects.toThrow('Binary');
 await expect(readInputFile(file('notes.txt','  '))).rejects.toThrow('empty');
});
it('rejects invalid UTF8 rather than silently corrupting source evidence',async()=>{
 await expect(readInputFile({name:'notes.txt',size:1,arrayBuffer:async()=>new Uint8Array([255]).buffer})).rejects.toThrow('UTF-8');
});
