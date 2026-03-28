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
      const text = new TextDecoder().decode(data.contents);
      console.log(text);
    } else {
      console.log('未找到，原始数据:', data);
    }
  };
};
```

在输出的 PlayerPrefs 内容中查找包含 `privatekey` 或 `session` 的字段，复制对应的 base58 字符串。

### 4. 也可以手动查看

1. 开发者工具 → **Application** 标签页
2. 左侧 **IndexedDB** → 展开 `/idbfs`
3. 点击 `FILE_DATA`
4. 找到 key 为 `/idbfs/33449c7f0276bb3b68e51256fd0ddcb8/PlayerPrefs` 的条目
5. 查看 `contents` 字段中的私钥

### 5. 填入 .env 文件

```
COLONY_PRIVATE_KEY=你的base58私钥
```

### 6. 验证

```bash
python colony_onchain.py verify
```

输出 `[OK] 匹配` 即表示私钥正确。

## 注意事项

- Session Key 可能会过期，如果报错 `密钥不匹配`，需要重新提取
- 私钥请妥善保管，不要泄露
