# 获取 PlayColony 私钥

PlayColony 是 Unity WebGL 游戏，Session Key 存储在浏览器 IndexedDB 的 `/idbfs` 数据库中。

## 步骤

### 1. 打开游戏页面并登录

在浏览器中打开 PlayColony 游戏并正常登录。

### 2. 打开开发者工具

按 `F12`，切换到 **Console（控制台）** 标签页。

### 3. 执行以下代码提取私钥

```javascript
const req = indexedDB.open('/idbfs');
req.onsuccess = () => {
  const db = req.result;
  const tx = db.transaction('FILE_DATA', 'readonly');
  const store = tx.objectStore('FILE_DATA');
  const key = '/idbfs/33449c7f0276bb3b68e51256fd0ddcb8/PlayerPrefs';
  const get = store.get(key);
  get.onsuccess = () => {
    const data = get.result;
    if (data && data.contents) {
      const raw = new Uint8Array(data.contents);
      const text = new TextDecoder().decode(raw);
      // Unity PlayerPrefs 格式：每个值前面有一个长度前缀字节
      // 需要按 key 名查找，然后跳过长度前缀读取值
      const keys = ['session.privatekey', 'session.publickey'];
      for (const k of keys) {
        const idx = text.indexOf(k);
        if (idx < 0) continue;
        const valueStart = idx + k.length;
        // valueStart 位置的字符是长度前缀（ASCII 值 = 字符串长度）
        const len = raw[new TextEncoder().encode(text.substring(0, valueStart)).length];
        const value = text.substring(valueStart + 1, valueStart + 1 + len);
        console.log(k + ' = ' + value);
      }
    } else {
      console.log('未找到，原始数据:', data);
    }
  };
};
```

### 4. 填入 .env 文件

将打印出的 `session.privatekey` 值（不含长度前缀）填入：

```
COLONY_PRIVATE_KEY=你的base58私钥
```

### 5. 验证

```bash
python colony_onchain.py discover
```

输出的公钥应与 `session.publickey` 一致。

## 注意事项

- Session Key 可能会过期，如果报错 `密钥不匹配`，需要重新提取
- 私钥请妥善保管，不要泄露
- Unity PlayerPrefs 中每个值前面有一个**长度前缀字节**（该字节的 ASCII 值等于后面字符串的长度），提取时需要跳过
