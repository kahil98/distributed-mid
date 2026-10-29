import { useCallback, useEffect, useMemo, useState } from 'react'
import './App.css'

const API = import.meta.env.VITE_GATEWAY_URL || 'http://127.0.0.1:8080'
const prettyBytes = (value = 0) => {
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']; let size = value / 1024; let index = 0
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1 }
  return `${size.toFixed(1)} ${units[index]}`
}
const prettyDate = (value) => value ? new Date(value).toLocaleString() : '—'

async function api(path, token, options = {}) {
  const headers = new Headers(options.headers || {})
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API}${path}`, { ...options, headers })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `Request failed (${response.status})`)
  }
  if (response.status === 204) return null
  return response
}

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem('gateway-token') || '')
  const [username, setUsername] = useState('exam-user')
  const [password, setPassword] = useState('change-me')
  const [tab, setTab] = useState('Overview')
  const [buckets, setBuckets] = useState([])
  const [bucket, setBucket] = useState('')
  const [objects, setObjects] = useState([])
  const [cluster, setCluster] = useState({ nodes: [], degraded_objects: [], repair_progress: [] })
  const [prefix, setPrefix] = useState('')
  const [tagKey, setTagKey] = useState('')
  const [tagValue, setTagValue] = useState('')
  const [minSize, setMinSize] = useState('')
  const [maxSize, setMaxSize] = useState('')
  const [createdAfter, setCreatedAfter] = useState('')
  const [createdBefore, setCreatedBefore] = useState('')
  const [file, setFile] = useState(null)
  const [objectKey, setObjectKey] = useState('')
  const [tagsText, setTagsText] = useState('{}')
  const [newBucket, setNewBucket] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const loadCluster = useCallback(async (auth = token) => {
    if (!auth) return
    const response = await api('/v1/cluster/status', auth)
    setCluster(await response.json())
  }, [token])

  const search = useCallback(async (auth = token, selectedBucket = bucket) => {
    if (!auth || !selectedBucket) { setObjects([]); return }
    const params = new URLSearchParams()
    if (prefix) params.set('prefix', prefix)
    if (minSize) params.set('min_size', minSize)
    if (maxSize) params.set('max_size', maxSize)
    if (createdAfter) params.set('created_after', new Date(createdAfter).toISOString())
    if (createdBefore) params.set('created_before', new Date(createdBefore).toISOString())
    if (tagKey && tagValue) params.set('tags', JSON.stringify({ [tagKey]: tagValue }))
    const response = await api(`/v1/buckets/${encodeURIComponent(selectedBucket)}/objects?${params}`, auth)
    setObjects((await response.json()).objects)
  }, [token, bucket, prefix, minSize, maxSize, createdAfter, createdBefore, tagKey, tagValue])

  const refresh = useCallback(async (auth = token) => {
    if (!auth) return
    const [response, clusterResponse] = await Promise.all([api('/v1/buckets', auth), api('/v1/cluster/status', auth)])
    const data = await response.json()
    setBuckets(data)
    setCluster(await clusterResponse.json())
    setBucket((selected) => selected && data.some((item) => item.bucket === selected) ? selected : data[0]?.bucket || '')
  }, [token])

  useEffect(() => {
    if (!token) return
    refresh().catch((cause) => setError(cause.message))
    const timer = window.setInterval(() => loadCluster().catch(() => {}), 8000)
    return () => window.clearInterval(timer)
  }, [token, refresh, loadCluster])

  useEffect(() => {
    const timer = token && bucket ? window.setTimeout(() => search().catch((cause) => setError(cause.message)), 350) : null
    return () => { if (timer) window.clearTimeout(timer) }
  }, [token, bucket, search])

  const login = async (event) => {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const response = await fetch(`${API}/auth/token`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }) })
      const body = await response.json()
      if (!response.ok) throw new Error(body.detail || 'Sign in failed')
      sessionStorage.setItem('gateway-token', body.access_token); setToken(body.access_token); setNotice('Signed in successfully')
    } catch (cause) { setError(cause.message) } finally { setBusy(false) }
  }

  const createBucket = async (event) => {
    event.preventDefault(); if (!newBucket.trim()) return
    setBusy(true); setError('')
    try {
      const response = await api('/v1/buckets', token, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bucket: newBucket.trim() }) })
      const created = await response.json(); setNewBucket(''); setNotice(`Bucket ${created.bucket} created`); await refresh(token); setBucket(created.bucket)
      await search(token, created.bucket)
    } catch (cause) { setError(cause.message) } finally { setBusy(false) }
  }

  const upload = async (event) => {
    event.preventDefault()
    if (!file || !bucket) { setError('Choose a bucket and a file first'); return }
    setBusy(true); setError('')
    try {
      const key = objectKey.trim() || file.name
      const parsedTags = JSON.parse(tagsText || '{}')
      const response = await api(`/v1/buckets/${encodeURIComponent(bucket)}/objects/${key.split('/').map(encodeURIComponent).join('/')}`, token,
        { method: 'PUT', headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Object-Tags': JSON.stringify(parsedTags) }, body: file })
      await response.json(); setNotice(`Uploaded ${key}`); setFile(null); setObjectKey(''); await refresh(token)
    } catch (cause) { setError(cause instanceof SyntaxError ? 'Tags must be valid JSON' : cause.message) } finally { setBusy(false) }
  }

  const download = async (object) => {
    setError('')
    try {
      const response = await api(`/v1/download/${encodeURIComponent(object.bucket)}/${object.key.split('/').map(encodeURIComponent).join('/')}`, token)
      const blob = await response.blob(); const link = document.createElement('a'); link.href = URL.createObjectURL(blob)
      link.download = object.key.split('/').pop() || 'download'; link.click(); URL.revokeObjectURL(link.href)
    } catch (cause) { setError(cause.message) }
  }

  const removeObject = async (object) => {
    if (!window.confirm(`Delete ${object.key}?`)) return
    try {
      await api(`/v1/buckets/${encodeURIComponent(object.bucket)}/objects/${object.key.split('/').map(encodeURIComponent).join('/')}`, token, { method: 'DELETE' })
      setNotice(`Deleted ${object.key}`); await refresh(token)
    } catch (cause) { setError(cause.message) }
  }

  const repairSummary = useMemo(() => {
    const counts = Object.fromEntries((cluster.repair_progress || []).map((item) => [item.status, item.count]))
    const total = Object.values(counts).reduce((sum, number) => sum + number, 0)
    const complete = counts.complete || 0
    return { counts, total, complete, percent: total ? Math.round(complete / total * 100) : 0 }
  }, [cluster.repair_progress])

  if (!token) return <main className="login-shell"><div className="login-card"><Brand /><span className="eyebrow">OBJECT STORAGE CONSOLE</span><h1>Welcome back</h1><p>Sign in to browse objects and monitor your storage cluster.</p>
    <form onSubmit={login} className="login-form"><label>Username<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></label><label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label><button className="primary wide" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button></form>
    {error && <p className="feedback error">{error}</p>}<small>Local exam account defaults: exam-user / change-me</small></div></main>

  const healthyNodes = cluster.nodes.filter((node) => node.health === 'healthy').length
  return <div className="app-shell">
    <aside className="sidebar"><Brand /><div className="side-label">WORKSPACE</div><nav>{['Overview', 'Objects', 'Cluster'].map((item, index) => <button key={item} className={`nav-link ${tab === item ? 'active' : ''}`} onClick={() => setTab(item)}><span className="nav-icon">{['◫', '▤', '⌘'][index]}</span>{item}{item === 'Cluster' && cluster.degraded_count > 0 && <b className="nav-count">{cluster.degraded_count}</b>}</button>)}</nav><div className="sidebar-bottom"><span className="green-dot" /> Storage gateway online <a href="http://127.0.0.1:8080/docs" target="_blank" rel="noreferrer">API docs ↗</a></div></aside>
    <main className="main-area"><header className="topbar"><div className="breadcrumb">Storage <span>/</span> {tab}</div><div className="top-actions"><div className="live-pill"><i /> Live cluster</div><button className="avatar" title="Sign out" onClick={() => { sessionStorage.removeItem('gateway-token'); setToken(''); setBuckets([]); setObjects([]) }}>{username.slice(0, 1).toUpperCase()}</button></div></header>
      <div className="content-wrap"><div className="page-heading"><div><span className="eyebrow">DISTRIBUTED STORAGE</span><h1>{tab === 'Objects' ? 'Object browser' : tab === 'Cluster' ? 'Cluster health' : 'Overview'}</h1><p>Manage your objects and monitor the storage fabric.</p></div><div className="heading-status"><span className="green-dot" />{healthyNodes} / {cluster.node_count || 0} nodes healthy</div></div>
      {error && <div className="feedback error">{error}<button onClick={() => setError('')}>Dismiss</button></div>}{notice && <div className="feedback success">{notice}<button onClick={() => setNotice('')}>Dismiss</button></div>}
      {(tab === 'Overview' || tab === 'Cluster') && <section className="stat-grid"><Stat label="Storage nodes" value={`${healthyNodes} / ${cluster.node_count || 0}`} detail="Healthy heartbeats" icon="⌘" tone="blue" /><Stat label="Objects indexed" value={objects.length} detail={`In ${bucket || 'no bucket selected'}`} icon="▤" tone="violet" /><Stat label="Degraded objects" value={cluster.degraded_count || 0} detail="Need attention" icon="⚠" tone={(cluster.degraded_count || 0) ? 'amber' : 'green'} /><Stat label="Repair progress" value={`${repairSummary.percent}%`} detail={`${repairSummary.counts.running || 0} jobs running`} icon="⟳" tone="teal" /></section>}
      {(tab === 'Overview' || tab === 'Objects') && <><section className="panel upload-panel"><div className="panel-head"><div><span className="panel-kicker">INGEST</span><h2>Upload an object</h2><p>Files are streamed through the gateway into the distributed cluster.</p></div><span className="panel-icon">↑</span></div><form className="upload-form" onSubmit={upload}><label className="field">Bucket<select value={bucket} onChange={(event) => setBucket(event.target.value)}><option value="">Select bucket</option>{buckets.map((item) => <option key={item.bucket}>{item.bucket}</option>)}</select></label><label className="field">Object key<input placeholder="folder/file-name.ext (defaults to file name)" value={objectKey} onChange={(event) => setObjectKey(event.target.value)} /></label><label className="file-drop"><input type="file" onChange={(event) => setFile(event.target.files?.[0] || null)} /><span className="file-symbol">＋</span><strong>{file ? file.name : 'Choose a file to upload'}</strong><small>{file ? prettyBytes(file.size) : 'Any file type · streamed upload'}</small></label><label className="field tags-field">Tags <span>JSON key/value map</span><input value={tagsText} onChange={(event) => setTagsText(event.target.value)} /></label><button className="primary" disabled={busy || !file || !bucket}>{busy ? 'Uploading…' : 'Upload object'} <span>↑</span></button></form></section>
      <section className="panel"><div className="panel-head browser-head"><div><span className="panel-kicker">OBJECTS</span><h2>Browse and search</h2></div><form className="new-bucket" onSubmit={createBucket}><input placeholder="New bucket name" value={newBucket} onChange={(event) => setNewBucket(event.target.value)} /><button className="secondary" disabled={busy}>Create bucket</button></form></div><div className="search-grid"><label className="field">Bucket<select value={bucket} onChange={(event) => setBucket(event.target.value)}><option value="">Select bucket</option>{buckets.map((item) => <option key={item.bucket}>{item.bucket}</option>)}</select></label><label className="field">Key prefix<input placeholder="e.g. reports/" value={prefix} onChange={(event) => setPrefix(event.target.value)} /></label><label className="field">Min size (bytes)<input type="number" min="0" value={minSize} onChange={(event) => setMinSize(event.target.value)} /></label><label className="field">Max size (bytes)<input type="number" min="0" value={maxSize} onChange={(event) => setMaxSize(event.target.value)} /></label><label className="field">Created after<input type="datetime-local" value={createdAfter} onChange={(event) => setCreatedAfter(event.target.value)} /></label><label className="field">Created before<input type="datetime-local" value={createdBefore} onChange={(event) => setCreatedBefore(event.target.value)} /></label><label className="field">Tag name<input placeholder="department" value={tagKey} onChange={(event) => setTagKey(event.target.value)} /></label><label className="field">Tag value<input placeholder="finance" value={tagValue} onChange={(event) => setTagValue(event.target.value)} /></label><button className="secondary search-button" onClick={() => search().catch((cause) => setError(cause.message))}>Search metadata</button></div><ObjectTable objects={objects} onDownload={download} onDelete={removeObject} /></section></>}
      {(tab === 'Overview' || tab === 'Cluster') && <section className="panel cluster-panel"><div className="panel-head"><div><span className="panel-kicker">INFRASTRUCTURE</span><h2>Storage node distribution</h2><p>Live heartbeats, available capacity and managed fragment counts.</p></div><button className="icon-button" onClick={() => loadCluster().catch((cause) => setError(cause.message))}>↻ Refresh</button></div><div className="node-list">{cluster.nodes.map((node) => { const total = Math.max(1, ...cluster.nodes.map((entry) => entry.managed_fragment_count || entry.chunk_count || 0)); const chunks = node.managed_fragment_count ?? node.chunk_count ?? 0; return <div className="node-row" key={node.node_id}><span className={`node-indicator ${node.health}`} /><div className="node-name"><strong>{node.node_id}</strong><small>{node.address}</small></div><span className={`status-tag ${node.health}`}>{node.health}</span><div className="capacity"><div className="capacity-bar"><i style={{ width: `${Math.min(100, chunks / total * 100)}%` }} /></div><small>{chunks} fragments · {prettyBytes(node.managed_fragment_bytes || 0)}</small></div><div className="node-space"><strong>{prettyBytes(node.free_space)}</strong><small>free space</small></div></div>})}{!cluster.nodes.length && <Empty title="No node heartbeats yet" text="The gateway will show node status once the cluster reports in." />}</div></section>}
      {(tab === 'Overview' || tab === 'Cluster') && <section className="panel repair-panel"><div className="panel-head"><div><span className="panel-kicker">DURABILITY</span><h2>Repair activity</h2><p>Background recovery job totals from the metadata service.</p></div><span className="repair-total">{repairSummary.complete} / {repairSummary.total} complete</span></div><div className="progress-track"><i style={{ width: `${repairSummary.percent}%` }} /></div><div className="repair-stats">{[['Complete', repairSummary.counts.complete || 0, 'green'], ['Running', repairSummary.counts.running || 0, 'blue'], ['Queued', repairSummary.counts.pending || 0, 'amber'], ['Failed', repairSummary.counts.failed || 0, 'red']].map(([label, value, tone]) => <div key={label}><i className={`legend-dot ${tone}`} /><span>{label}</span><strong>{value}</strong></div>)}</div><div className="traffic-row"><span>Repair traffic <b>{prettyBytes(cluster.repair_traffic_bytes || 0)}</b></span><span>Scrub reads <b>{prettyBytes(cluster.scrub_read_bytes || 0)}</b></span></div></section>}
      {(tab === 'Overview' || tab === 'Cluster') && cluster.degraded_objects?.length > 0 && <section className="panel degraded-panel"><div className="panel-head"><div><span className="panel-kicker">ATTENTION</span><h2>Degraded objects</h2></div></div>{cluster.degraded_objects.map((item) => <div className="degraded-row" key={`${item.bucket}/${item.key}`}><span className="warning-mark">!</span><strong>{item.key}</strong><span>{item.bucket}</span><small>{prettyBytes(item.size)}</small></div>)}</section>}
      <footer>Distributed Object Storage <span>·</span> Search powered by Elasticsearch</footer></div>
    </main>
  </div>
}

function Brand() { return <div className="brand"><span className="brand-mark"><i /><i /><i /></span><span>strata<span className="brand-dot">.</span><small>OBJECT STORAGE</small></span></div> }
function Stat({ label, value, detail, icon, tone }) { return <article className="stat-card"><span className={`stat-icon ${tone}`}>{icon}</span><div><small>{label}</small><strong>{value}</strong><span>{detail}</span></div></article> }
function ObjectTable({ objects, onDownload, onDelete }) { return <div className="table-wrap"><table><thead><tr><th>OBJECT KEY</th><th>SIZE</th><th>VERSION</th><th>TAGS</th><th>CREATED</th><th /></tr></thead><tbody>{objects.map((item) => <tr key={`${item.bucket}/${item.key}`}><td><span className="file-type">▤</span><strong>{item.key}</strong></td><td>{prettyBytes(item.size)}</td><td><span className="version">v{item.version}</span></td><td><div className="tag-list">{Object.entries(item.tags || {}).slice(0, 2).map(([key, value]) => <span className="tag" key={key}>{key}: {value}</span>)}</div></td><td>{prettyDate(item.created_at)}</td><td className="row-actions"><button title="Download" onClick={() => onDownload(item)}>↓</button><button title="Delete" onClick={() => onDelete(item)}>×</button></td></tr>)}</tbody></table>{!objects.length && <Empty title="No objects found" text="Upload a file or adjust your search filters." />}</div> }
function Empty({ title, text }) { return <div className="empty-state"><span>⌕</span><strong>{title}</strong><small>{text}</small></div> }

export default App
