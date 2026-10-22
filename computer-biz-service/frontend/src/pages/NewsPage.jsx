import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { fetchNewsItem, fetchNewsItems, fetchNewsOverview } from '../api/news.js'

const { Text, Paragraph, Link } = Typography

// 状态与配色只在页面层定义：后端存的是英文枚举，翻译属于展示问题。
const STATUS_META = {
  new: { label: '待分类', color: 'default' },
  classified: { label: '已分类', color: 'blue' },
  filtered_out: { label: '已过滤', color: 'default' },
  failed: { label: '失败', color: 'red' },
  ingest_pending: { label: '待入库', color: 'gold' },
  ingested: { label: '已入库', color: 'green' },
  skipped: { label: '已跳过', color: 'orange' },
}

const STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  ...Object.entries(STATUS_META).map(([value, meta]) => ({
    value,
    label: meta.label,
  })),
]

const CATEGORY_META = {
  model_release: { label: '模型发布', color: 'geekblue' },
  product_update: { label: '产品更新', color: 'cyan' },
  other: { label: '其他', color: 'default' },
}

// 抓取时间统一是 UTC ISO（Python 侧带 Z），交给浏览器换成本地时区。
const formatTime = (value) => {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
}

const formatElapsed = (seconds) => {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—'
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} 分 ${Math.round(seconds % 60)} 秒`
}

/**
 * 只有 http(s) 才能变成链接。抓取内容来自外部站点，不能让 javascript:/data:
 * 通过「原文链接」进入页面。
 */
const safeHttpUrl = (value) => {
  if (typeof value !== 'string') return ''
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : ''
  } catch {
    return ''
  }
}

const statusTag = (status) => {
  const meta = STATUS_META[status]
  return <Tag color={meta?.color}>{meta?.label ?? status ?? '未知'}</Tag>
}

const categoryTag = (category) => {
  if (!category) return <Text type="secondary">—</Text>
  const meta = CATEGORY_META[category]
  return <Tag color={meta?.color}>{meta?.label ?? category}</Tag>
}

const importanceText = (importance) =>
  typeof importance === 'number' ? `${importance} / 5` : '—'

export default function NewsPage() {
  const [overview, setOverview] = useState(null)
  const [overviewLoading, setOverviewLoading] = useState(false)
  const [overviewError, setOverviewError] = useState('')

  const [items, setItems] = useState({ items: [], total: 0, page: 1, pageSize: 20 })
  const [itemsLoading, setItemsLoading] = useState(false)
  const [itemsError, setItemsError] = useState('')

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [status, setStatus] = useState('all')
  const [sourceId, setSourceId] = useState('')
  const [query, setQuery] = useState('')
  const [searchInput, setSearchInput] = useState('')

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')

  // 快速切换筛选/连续点两篇文章时，只接受最后一次请求的结果。
  const itemsReqRef = useRef(0)
  const detailReqRef = useRef(0)

  const loadOverview = useCallback(async () => {
    setOverviewLoading(true)
    setOverviewError('')
    try {
      setOverview(await fetchNewsOverview(12))
    } catch (err) {
      setOverviewError(err?.message || '加载抓取统计失败')
    } finally {
      setOverviewLoading(false)
    }
  }, [])

  const loadItems = useCallback(async () => {
    const reqId = ++itemsReqRef.current
    setItemsLoading(true)
    setItemsError('')
    try {
      const data = await fetchNewsItems({
        status: status === 'all' ? '' : status,
        sourceId,
        q: query,
        page,
        pageSize,
      })
      if (itemsReqRef.current === reqId) setItems(data)
    } catch (err) {
      if (itemsReqRef.current === reqId) {
        setItemsError(err?.message || '加载抓取内容失败')
      }
    } finally {
      if (itemsReqRef.current === reqId) setItemsLoading(false)
    }
  }, [status, sourceId, query, page, pageSize])

  useEffect(() => {
    loadOverview()
  }, [loadOverview])

  useEffect(() => {
    loadItems()
  }, [loadItems])

  const openDetail = async (record) => {
    const reqId = ++detailReqRef.current
    setDrawerOpen(true)
    setDetail(record) // 先用列表数据把标题/元信息铺上，详情回来再补正文
    setDetailError('')
    setDetailLoading(true)
    try {
      const data = await fetchNewsItem(record.id)
      if (detailReqRef.current === reqId) setDetail(data)
    } catch (err) {
      if (detailReqRef.current === reqId) {
        setDetailError(err?.message || '加载文章详情失败')
      }
    } finally {
      if (detailReqRef.current === reqId) setDetailLoading(false)
    }
  }

  const handleRefresh = () => {
    loadOverview()
    loadItems()
  }

  const counts = overview?.counts ?? {}
  const pending =
    (counts.new ?? 0) + (counts.classified ?? 0) + (counts.ingest_pending ?? 0)
  const lastRun = overview?.runs?.[0]
  const sourceOptions = (overview?.sources ?? []).map((source) => ({
    value: source.sourceId,
    label: source.name || source.sourceId,
  }))
  const dbMissing = overview != null && !overview.available

  const columns = [
    {
      title: '标题',
      dataIndex: 'titleZh',
      key: 'title',
      width: 360,
      render: (_, record) => (
        <div className="news-title-cell">
          <div className="news-title-cell__main">
            {record.titleZh || record.title || '（无标题）'}
          </div>
          {record.titleZh && record.title && (
            <div className="news-title-cell__original">{record.title}</div>
          )}
          {record.excerpt && (
            <div className="news-title-cell__excerpt">{record.excerpt}</div>
          )}
        </div>
      ),
    },
    {
      title: '来源',
      dataIndex: 'sourceName',
      key: 'source',
      width: 140,
      render: (_, record) => (
        <div className="news-source-cell">
          <div>{record.sourceName || record.sourceId || '—'}</div>
          <code className="news-source-cell__id">{record.sourceId}</code>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 92,
      render: (value) => statusTag(value),
    },
    {
      title: '分类',
      dataIndex: 'category',
      key: 'category',
      width: 100,
      render: (value) => categoryTag(value),
    },
    {
      title: '重要度',
      dataIndex: 'importance',
      key: 'importance',
      width: 84,
      render: (value) => importanceText(value),
    },
    {
      title: '发布时间',
      dataIndex: 'publishedAt',
      key: 'publishedAt',
      width: 160,
      render: (value) => formatTime(value),
    },
    {
      title: '首次发现',
      dataIndex: 'fetchedAt',
      key: 'fetchedAt',
      width: 160,
      render: (value) => formatTime(value),
    },
    {
      title: '操作',
      key: 'action',
      width: 76,
      fixed: 'right',
      render: (_, record) => (
        <Button type="link" size="small" onClick={() => openDetail(record)}>
          查看
        </Button>
      ),
    },
  ]

  const runColumns = [
    {
      title: '开始时间',
      dataIndex: 'startedAt',
      key: 'startedAt',
      width: 160,
      render: (value) => formatTime(value),
    },
    {
      title: '耗时',
      dataIndex: 'elapsedS',
      key: 'elapsedS',
      width: 100,
      render: (value) => formatElapsed(value),
    },
    {
      title: '模式',
      dataIndex: 'mode',
      key: 'mode',
      width: 84,
      render: (value) => value || '—',
    },
    {
      title: '退出码',
      dataIndex: 'exitCode',
      key: 'exitCode',
      width: 84,
      render: (value) =>
        value == null ? (
          <Text type="secondary">运行中</Text>
        ) : (
          <Tag color={value === 0 ? 'green' : 'red'}>{value}</Tag>
        ),
    },
    { title: '抓取', dataIndex: 'fetched', key: 'fetched', width: 64 },
    { title: '候选', dataIndex: 'candidates', key: 'candidates', width: 64 },
    { title: '过滤', dataIndex: 'filtered', key: 'filtered', width: 64 },
    {
      title: '过期',
      dataIndex: 'expired',
      key: 'expired',
      width: 64,
      render: (value) => <Text type="secondary">{value ?? 0}</Text>,
    },
    {
      title: '入库',
      dataIndex: 'ingested',
      key: 'ingested',
      width: 64,
      render: (value) => <Text type={value > 0 ? 'success' : undefined}>{value}</Text>,
    },
    { title: '跳过', dataIndex: 'skipped', key: 'skipped', width: 64 },
    {
      title: '失败',
      dataIndex: 'failed',
      key: 'failed',
      width: 64,
      render: (value) => <Text type={value > 0 ? 'danger' : undefined}>{value}</Text>,
    },
    {
      title: '数据源',
      key: 'sources',
      width: 100,
      render: (_, record) => (
        <span>
          {record.sourcesOk} 成功
          {record.sourcesFailed > 0 && (
            <Text type="danger"> / {record.sourcesFailed} 失败</Text>
          )}
        </span>
      ),
    },
    {
      title: '运行 ID',
      dataIndex: 'runId',
      key: 'runId',
      width: 210,
      render: (value) => <code className="news-run-id">{value}</code>,
    },
  ]

  const runSourceColumns = [
    {
      title: '数据源',
      dataIndex: 'sourceName',
      key: 'sourceName',
      render: (_, record) => record.sourceName || record.sourceId,
    },
    { title: '抓取', dataIndex: 'fetched', key: 'fetched', width: 64 },
    { title: '候选', dataIndex: 'candidates', key: 'candidates', width: 64 },
    { title: '过滤', dataIndex: 'filtered', key: 'filtered', width: 64 },
    { title: '入库', dataIndex: 'ingested', key: 'ingested', width: 64 },
    { title: '跳过', dataIndex: 'skipped', key: 'skipped', width: 64 },
    {
      title: '失败',
      dataIndex: 'failed',
      key: 'failed',
      width: 64,
      render: (value) => <Text type={value > 0 ? 'danger' : undefined}>{value}</Text>,
    },
    {
      title: '错误',
      dataIndex: 'error',
      key: 'error',
      render: (value) => (value ? <Text type="danger">{value}</Text> : '—'),
    },
  ]

  const sourceColumns = [
    {
      title: '数据源',
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => (
        <div className="news-source-cell">
          <div>{record.name || record.sourceId}</div>
          <code className="news-source-cell__id">{record.sourceId}</code>
        </div>
      ),
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 76,
      render: (value) =>
        value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>,
    },
    {
      title: '最近抓取',
      dataIndex: 'lastFetchedAt',
      key: 'lastFetchedAt',
      width: 160,
      render: (value) => formatTime(value),
    },
    {
      title: 'HTTP',
      dataIndex: 'lastStatus',
      key: 'lastStatus',
      width: 76,
      render: (value) =>
        value == null ? (
          '—'
        ) : (
          <Tag color={value >= 200 && value < 300 ? 'green' : 'red'}>{value}</Tag>
        ),
    },
    {
      title: '连续失败',
      dataIndex: 'consecutiveFailures',
      key: 'consecutiveFailures',
      width: 90,
      render: (value) => (value > 0 ? <Text type="danger">{value}</Text> : '0'),
    },
    {
      title: '最近错误',
      dataIndex: 'lastError',
      key: 'lastError',
      render: (value) => (value ? <Text type="danger">{value}</Text> : '—'),
    },
  ]

  const lastRunSummary = lastRun ? (
    <div className="news-last-run">
      <div className="news-last-run__time">{formatTime(lastRun.startedAt)}</div>
      <Space size={4} wrap>
        {lastRun.exitCode == null ? (
          <Tag color="processing">运行中</Tag>
        ) : (
          <Tag color={lastRun.exitCode === 0 ? 'green' : 'red'}>
            退出码 {lastRun.exitCode}
          </Tag>
        )}
        <Text type="secondary">入库 {lastRun.ingested}</Text>
      </Space>
    </div>
  ) : (
    <Text type="secondary">还没有运行记录</Text>
  )

  const itemsTab = (
    <div>
      {itemsError && (
        <Alert
          type="error"
          showIcon
          message="抓取内容加载失败"
          description={itemsError}
          style={{ marginBottom: 12 }}
        />
      )}

      <div className="news-toolbar">
        <Space wrap size={8}>
          <Select
            value={status}
            options={STATUS_OPTIONS}
            onChange={(value) => {
              setStatus(value)
              setPage(1)
            }}
            style={{ width: 128 }}
          />
          <Select
            value={sourceId || undefined}
            allowClear
            placeholder="全部来源"
            options={sourceOptions}
            onChange={(value) => {
              setSourceId(value ?? '')
              setPage(1)
            }}
            style={{ width: 180 }}
          />
          <Input.Search
            value={searchInput}
            allowClear
            placeholder="搜索标题或摘要"
            onChange={(event) => {
              const value = event.target.value
              setSearchInput(value)
              // 点 clear 时立即回到全量列表，不用再按回车
              if (!value && query) {
                setQuery('')
                setPage(1)
              }
            }}
            onSearch={(value) => {
              setQuery(value.trim())
              setPage(1)
            }}
            style={{ width: 240 }}
          />
        </Space>
        <Button
          icon={<ReloadOutlined />}
          loading={overviewLoading || itemsLoading}
          onClick={handleRefresh}
        >
          刷新
        </Button>
      </div>

      <Table
        rowKey="id"
        size="middle"
        loading={itemsLoading}
        columns={columns}
        dataSource={items.items}
        scroll={{ x: 1080 }}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={dbMissing ? '状态库还不存在' : '没有符合条件的抓取记录'}
            />
          ),
        }}
        pagination={{
          current: page,
          pageSize,
          total: items.total,
          showSizeChanger: true,
          pageSizeOptions: ['10', '20', '50', '100'],
          showTotal: (total) => `共 ${total} 篇`,
          onChange: (nextPage, nextPageSize) => {
            if (nextPageSize !== pageSize) {
              setPageSize(nextPageSize)
              setPage(1)
            } else {
              setPage(nextPage)
            }
          },
        }}
      />
    </div>
  )

  const runsTab = (
    <div>
      {overviewError && (
        <Alert
          type="error"
          showIcon
          message="运行记录加载失败"
          description={overviewError}
          style={{ marginBottom: 12 }}
        />
      )}

      <Card
        size="small"
        title="数据源状态"
        className="news-card"
        extra={<Text type="secondary">最近一次抓取的结果</Text>}
      >
        <Table
          rowKey="sourceId"
          size="small"
          columns={sourceColumns}
          dataSource={overview?.sources ?? []}
          pagination={false}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据源" /> }}
          scroll={{ x: 760 }}
        />
      </Card>

      <Card
        size="small"
        title="运行记录"
        className="news-card"
        extra={<Text type="secondary">每轮运行独立统计，不与文章强行关联</Text>}
      >
        <Table
          rowKey="runId"
          size="small"
          loading={overviewLoading}
          columns={runColumns}
          dataSource={overview?.runs ?? []}
          pagination={false}
          scroll={{ x: 1120 }}
          locale={{
            emptyText: (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有运行记录" />
            ),
          }}
          expandable={{
            rowExpandable: (record) => record.sources.length > 0,
            expandedRowRender: (record) => (
              <div className="news-run-detail">
                <Table
                  rowKey="sourceId"
                  size="small"
                  columns={runSourceColumns}
                  dataSource={record.sources}
                  pagination={false}
                />
                {record.fatalError && (
                  <Alert
                    type="error"
                    showIcon
                    message="运行错误"
                    description={record.fatalError}
                    style={{ marginTop: 8 }}
                  />
                )}
              </div>
            ),
          }}
        />
      </Card>
    </div>
  )

  const contentUrl = safeHttpUrl(detail?.canonicalUrl)
  const detailItems = detail
    ? [
        { key: 'source', label: '来源', children: `${detail.sourceName || detail.sourceId || '—'}（${detail.sourceId || '—'}）` },
        { key: 'status', label: '状态', children: statusTag(detail.status) },
        { key: 'category', label: '分类', children: categoryTag(detail.category) },
        { key: 'importance', label: '重要度', children: importanceText(detail.importance) },
        { key: 'published', label: '发布时间', children: formatTime(detail.publishedAt) },
        { key: 'fetched', label: '首次发现', children: formatTime(detail.fetchedAt) },
        ...(detail.updatedAt
          ? [{ key: 'updated', label: '记录更新', children: formatTime(detail.updatedAt) }]
          : []),
        {
          key: 'url',
          label: '原文',
          children: contentUrl ? (
            <Link href={contentUrl} target="_blank" rel="noopener noreferrer">
              {contentUrl}
            </Link>
          ) : (
            '—'
          ),
        },
        ...(detail.docFilename
          ? [{ key: 'doc', label: '入库文件', children: <code>{detail.docFilename}</code> }]
          : []),
        ...(detail.ingestResult
          ? [{ key: 'ingest', label: '入库结果', children: detail.ingestResult }]
          : []),
        ...(detail.jobId
          ? [{ key: 'job', label: '入库任务', children: <code>{detail.jobId}</code> }]
          : []),
        ...(detail.retryCount > 0
          ? [{ key: 'retry', label: '重试次数', children: detail.retryCount }]
          : []),
        ...(detail.nextRetryAt
          ? [{ key: 'nextRetry', label: '下次重试', children: formatTime(detail.nextRetryAt) }]
          : []),
        ...(detail.reason
          ? [{ key: 'reason', label: '过滤原因', children: detail.reason }]
          : []),
        ...(detail.lastError
          ? [{ key: 'error', label: '最近错误', children: <Text type="danger">{detail.lastError}</Text> }]
          : []),
      ]
    : []

  return (
    <div>
      {dbMissing && (
        <Alert
          type="warning"
          showIcon
          className="news-alert"
          message="还没有抓取记录"
          description={
            <span>
              状态库 <code>crawler_state/news.db</code> 尚未生成。先在 LightRAG 目录运行
              <code> python -m news_ingest --once</code>，或等定时任务跑过一轮后刷新本页。
            </span>
          }
        />
      )}

      {overviewError && !dbMissing && (
        <Alert
          type="error"
          showIcon
          className="news-alert"
          message="抓取动态加载失败"
          description={overviewError}
        />
      )}

      <Row gutter={[12, 12]} className="news-stats">
        <Col xs={12} sm={8} lg={4}>
          <Card size="small">
            <Statistic title="累计发现" value={counts.total ?? 0} suffix="篇" />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small">
            <Statistic
              title="已入库"
              value={counts.ingested ?? 0}
              suffix="篇"
              valueStyle={{ color: '#389e0d' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small">
            <Statistic title="待处理" value={pending} suffix="篇" />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small">
            <Statistic title="已过滤" value={counts.filtered_out ?? 0} suffix="篇" />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small">
            <Statistic
              title="失败"
              value={counts.failed ?? 0}
              suffix="篇"
              valueStyle={{ color: (counts.failed ?? 0) > 0 ? '#cf1322' : undefined }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small" title="最近运行" className="news-last-run-card">
            {lastRunSummary}
          </Card>
        </Col>
      </Row>

      <Paragraph type="secondary" className="news-note">
        文章列表按「首次发现时间」排序；运行记录单独统计，两者没有可展示的逐篇归属关系。
        {overview?.generatedAt && ` 数据读取于 ${formatTime(overview.generatedAt)}。`}
      </Paragraph>

      <Tabs
        items={[
          { key: 'items', label: `抓取内容（${items.total}）`, children: itemsTab },
          { key: 'runs', label: '运行记录', children: runsTab },
        ]}
      />

      <Drawer
        open={drawerOpen}
        onClose={() => {
          detailReqRef.current += 1 // 关掉后，迟到的响应不许再改状态
          setDrawerOpen(false)
        }}
        width={760}
        title={
          <div className="news-drawer-title">
            {detail?.titleZh || detail?.title || `文章 #${detail?.id ?? ''}`}
          </div>
        }
        extra={
          contentUrl ? (
            <Button href={contentUrl} target="_blank" rel="noopener noreferrer">
              打开原文
            </Button>
          ) : null
        }
      >
        {detailError && (
          <Alert
            type="error"
            showIcon
            message="文章详情加载失败"
            description={detailError}
            style={{ marginBottom: 12 }}
          />
        )}

        <Descriptions
          size="small"
          bordered
          column={1}
          items={detailItems}
          className="news-detail-meta"
        />

        {detailLoading && (
          <Paragraph type="secondary" style={{ marginTop: 12 }}>
            正在读取正文…
          </Paragraph>
        )}

        {detail?.summaryZh && (
          <section className="news-detail-section">
            <Typography.Title level={5}>中文摘要</Typography.Title>
            <Paragraph className="news-detail-text">{detail.summaryZh}</Paragraph>
          </section>
        )}

        {detail?.keyFacts?.length > 0 && (
          <section className="news-detail-section">
            <Typography.Title level={5}>关键事实</Typography.Title>
            <ul className="news-facts">
              {detail.keyFacts.map((fact, index) => (
                <li key={index}>{fact}</li>
              ))}
            </ul>
          </section>
        )}

        <section className="news-detail-section">
          <Typography.Title level={5}>
            正文
            {detail?.contentKind === 'feed' && (
              <Tag style={{ marginLeft: 8 }}>来源摘要，未生成清洗正文</Tag>
            )}
            {detail?.contentKind === 'document' && (
              <Tag color="green" style={{ marginLeft: 8 }}>
                已清洗并入库
              </Tag>
            )}
          </Typography.Title>
          {detail?.content ? (
            <pre className="news-doc">{detail.content}</pre>
          ) : (
            !detailLoading && (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="这篇文章还没有可展示的正文"
              />
            )
          )}
        </section>
      </Drawer>
    </div>
  )
}
