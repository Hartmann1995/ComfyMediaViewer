/*
 * Gilbert traversal adapted from https://github.com/jakubcerveny/gilbert
 *
 * BSD 2-Clause License
 *
 * Copyright (c) 2018, Jakub Červený
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * * Redistributions of source code must retain the above copyright notice, this
 *   list of conditions and the following disclaimer.
 *
 * * Redistributions in binary form must reproduce the above copyright notice,
 *   this list of conditions and the following disclaimer in the documentation
 *   and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
 * SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
 * CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
 * OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

"use strict";

function gilbertOrder(width, height){
  const order = new Uint32Array(width * height);
  let index = 0;

  function walk(x, y, ax, ay, bx, by){
    const w = Math.abs(ax + ay), h = Math.abs(bx + by);
    const dax = Math.sign(ax), day = Math.sign(ay);
    const dbx = Math.sign(bx), dby = Math.sign(by);
    if(h === 1 || w === 1){
      const count = h === 1 ? w : h;
      const step = h === 1 ? dax + day * width : dbx + dby * width;
      let pixel = y * width + x;
      for(let i = 0; i < count; i++, pixel += step) order[index++] = pixel;
      return;
    }

    let ax2 = Math.floor(ax / 2), ay2 = Math.floor(ay / 2);
    let bx2 = Math.floor(bx / 2), by2 = Math.floor(by / 2);
    if(2 * w > 3 * h){
      if(Math.abs(ax2 + ay2) % 2 && w > 2){ ax2 += dax; ay2 += day; }
      walk(x, y, ax2, ay2, bx, by);
      walk(x + ax2, y + ay2, ax - ax2, ay - ay2, bx, by);
      return;
    }

    if(Math.abs(bx2 + by2) % 2 && h > 2){ bx2 += dbx; by2 += dby; }
    walk(x, y, bx2, by2, ax2, ay2);
    walk(x + bx2, y + by2, ax, ay, bx - bx2, by - by2);
    walk(x + ax - dax + bx2 - dbx, y + ay - day + by2 - dby,
         -bx2, -by2, -(ax - ax2), -(ay - ay2));
  }

  if(width >= height) walk(0, 0, width, 0, 0, height);
  else walk(0, 0, 0, height, width, 0);
  return order;
}

function scramblePixels(input, output, width, height, reverse){
  const order = gilbertOrder(width, height);
  const total = order.length;
  const offset = Math.round((Math.sqrt(5) - 1) / 2 * width * height) % total;
  let shifted = reverse ? (total - offset) % total : offset;
  for(let i = 0; i < total; i++){
    output[order[shifted]] = input[order[i]];
    if(++shifted === total) shifted = 0;
  }
}

async function hasAnimation(blob){
  const header = new DataView(await blob.slice(0, 21).arrayBuffer());
  if(header.byteLength >= 3 && header.getUint16(0) === 0x4749 && header.getUint8(2) === 0x46)
    return true; // GIF，含被改过后缀的文件。
  if(header.byteLength >= 21 && header.getUint32(0) === 0x52494646
      && header.getUint32(8) === 0x57454250 && header.getUint32(12) === 0x56503858)
    return !!(header.getUint8(20) & 0x02); // WebP 的 VP8X 动画标志位。

  if(header.byteLength < 8 || header.getUint32(0) !== 0x89504e47
      || header.getUint32(4) !== 0x0d0a1a0a) return false;
  // APNG 的 acTL 必须位于第一个 IDAT 之前。跳过像素和元数据块的内容。
  let offset = 8;
  while(offset + 12 <= blob.size){
    const chunk = new DataView(await blob.slice(offset, offset + 8).arrayBuffer());
    const kind = chunk.getUint32(4);
    if(kind === 0x6163544c) return true; // acTL
    if(kind === 0x49444154 || kind === 0x49454e44) break; // IDAT / IEND
    offset += chunk.getUint32(0) + 12;
  }
  return false;
}

self.onmessage = async ({data: {url, width, height, reverse}}) => {
  try{
    const response = await fetch(url);
    if(!response.ok) throw new Error("读取当前图片失败");
    const blob = await response.blob();
    if(await hasAnimation(blob)) throw new Error("混淆与解混淆只支持静态图片");
    const image = await createImageBitmap(blob);
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext("2d", {willReadFrequently: true});
    try{
      if(image.width !== width || image.height !== height)
        throw new Error("图片尺寸在处理前发生变化");
      ctx.drawImage(image, 0, 0);
    }finally{
      image.close();
    }
    const source = ctx.getImageData(0, 0, width, height);
    const result = ctx.createImageData(width, height);
    scramblePixels(new Uint32Array(source.data.buffer), new Uint32Array(result.data.buffer),
                   width, height, reverse);
    ctx.putImageData(result, 0, 0);
    self.postMessage({blob: await canvas.convertToBlob({type: "image/png"})});
  }catch(error){
    self.postMessage({error: error.message || "处理失败"});
  }
};
