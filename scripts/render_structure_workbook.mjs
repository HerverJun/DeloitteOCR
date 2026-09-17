import {pathToFileURL} from 'node:url';
import fs from 'node:fs/promises';
const [modulePath,workbook,output]=process.argv.slice(2);
const {FileBlob,SpreadsheetFile}=await import(pathToFileURL(modulePath).href);
const book=await SpreadsheetFile.importXlsx(await FileBlob.load(workbook));
const preview=await book.render({sheetName:'Table 1',range:'A1:L14',scale:1.5,format:'png'});
await fs.writeFile(output,new Uint8Array(await preview.arrayBuffer()));
console.log(JSON.stringify({workbook,output,operation:'read-only render; workbook unchanged'}));
