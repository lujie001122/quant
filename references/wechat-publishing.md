# WeChat Official Account Publishing Setup

## Tool: md2wechat v2.0.1

Installed in ~/hermes-trading/.venv/. Converts Markdown → WeChat HTML → drafts API.

### Installation (already done)

```bash
source ~/hermes-trading/.venv/bin/activate
pip install md2wechat -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Credentials

Stored at `~/hermes/scripts/.env.wechat`:
```
WECHAT_APPID=wx178e97b3d60f3de7
WECHAT_APP_SECRET=***
```

Run with env vars:
```bash
source ~/hermes-trading/.venv/bin/activate
WECHAT_APPID=wx178e97b3d60f3de7 WECHAT_APP_SECRET=*** \
  md2wechat --markdown article.md --style tech --title "Title"
```

### Visual Styles

| Style | Name | Best For |
|-------|------|----------|
| `academic_gray` | 学术灰 | 技术文档、学术论文 |
| `tech` | 科技蓝 | 产品介绍、科技文章 |
| `festival` | 节日红金 | 节日祝福、庆祝内容 |
| `announcement` | 公告橙红 | 重要通知、公告 |

### Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| 40164 | IP not in whitelist | Add IP to mp.weixin.qq.com whitelist |
| 40001 | AppSecret invalid | Reset AppSecret, update .env |
| 40013 | AppID invalid | Must start with `wx` |
| MISSING_COVER_IMAGE | No images in article | Add at least one `![alt](path)` |

### Requirements

1. **IP whitelist**: must add server's public IP at mp.weixin.qq.com → 设置 → 开发 → 基本配置 → IP白名单
2. **Cover image**: first image in markdown is used as cover. WeChat requires this.
3. **Drafts only**: md2wechat never auto-publishes. Articles go to drafts for manual review.
4. **Verified account**: must be a verified service or subscription account to use drafts API.

### Subscription Account Limitations (未认证订阅号)

The user's account is an unverified subscription account. Many APIs return errcode 48001:

| API | Status | Note |
|-----|--------|------|
| Draft (create/list) | ✅ Works | Core functionality |
| Comment open/close | ❌ 48001 | Must manually enable in WeChat backend |
| Original declaration (原创声明) | ❌ 48001 | Must manually enable when publishing |
| Freepublish | ❌ 48001 | Must manually publish from drafts |
| User tags | ❌ 48001 | Not available |

**Workaround**: md2wechat `--comment` flag won't take effect. Remind user to manually enable comments + original declaration in WeChat backend after publishing.

**To get full API access**: certify as a service account (认证服务号, 300元/年, even individual businesses qualify).

### Article Style Preference

User rejected first draft as "太虚了" (too vague/hype). Requirements:
- Practical, actionable content with real code snippets and real numbers
- Include GitHub repo link for interested readers
- No marketing language or vague claims
- Show real pitfalls and how to fix them

### Integration with ETF Daily Review

The ETF复盘 cron job can generate a markdown report and push to drafts:
1. Generate markdown report from signal_generator / portfolio data
2. Add a cover image (chart or generated graphic)
3. Run md2wechat with `--style tech`
4. User reviews in WeChat drafts and publishes manually
