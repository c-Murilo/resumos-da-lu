import { StrictMode, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './styles.css'

const API = import.meta.env.VITE_API_URL || 'http://localhost:5001/api'
const POLL_MS = 5 * 60 * 1000

// Erro de gateway (502/504) devolve HTML, e `response.json()` estoura com
// "Unexpected token '<'", que não diz nada. Aqui vira uma mensagem legível.
async function lerResposta(response) {
  const texto = await response.text()
  try {
    const corpo = JSON.parse(texto)
    if (!response.ok) throw new Error(corpo.error || `Erro ${response.status} no servidor.`)
    return corpo
  } catch (err) {
    if (err instanceof SyntaxError) {
      throw new Error(response.status === 504
        ? 'O servidor demorou demais ou reiniciou (504). Veja os logs do deploy.'
        : `O servidor respondeu ${response.status} em vez de JSON.`)
    }
    throw err
  }
}

function date(value) {
  return value ? new Intl.DateTimeFormat('pt-BR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : 'Sem data'
}

function Sparkle() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2l1.7 6.3L20 10l-6.3 1.7L12 18l-1.7-6.3L4 10l6.3-1.7L12 2Zm7.1 13.1.7 2.2 2.2.7-2.2.7-.7 2.2-.7-2.2-2.2-.7 2.2-.7.7-2.2Z" /></svg> }
function Play() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 11 7-11 7V5Z" /></svg> }

function Slides({ slides, index, onIndex }) {
  const current = slides[Math.min(index, slides.length - 1)]
  const go = step => onIndex(Math.min(Math.max(index + step, 0), slides.length - 1))

  return <div className="slides">
    <div className="slides-head">
      <h3>Slides de revisão</h3>
      <div className="slides-nav">
        <button onClick={() => go(-1)} disabled={index === 0} aria-label="Slide anterior">←</button>
        <span>{Math.min(index, slides.length - 1) + 1} / {slides.length}</span>
        <button onClick={() => go(1)} disabled={index >= slides.length - 1} aria-label="Próximo slide">→</button>
      </div>
    </div>
    {/* Na tela, um slide por vez; na impressão, o CSS revela todos. */}
    {slides.map((item, i) => <article className={`slide ${i === Math.min(index, slides.length - 1) ? 'is-current' : ''}`} key={i}>
      <h4>{item.titulo}</h4>
      <ul>{item.topicos?.map((topico, j) => <li key={j}>{topico}</li>)}</ul>
      {item.nota && <p className="slide-note">{item.nota}</p>}
      <span className="slide-count">{i + 1}</span>
    </article>)}
  </div>
}

function App() {
  const [recordings, setRecordings] = useState([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [chat, setChat] = useState([])
  const [question, setQuestion] = useState('')
  const [asking, setAsking] = useState(false)
  const [models, setModels] = useState([])
  const [model, setModel] = useState('')
  const [slide, setSlide] = useState(0)
  const [aba, setAba] = useState('plaud')
  const [salvos, setSalvos] = useState([])
  const chatEnd = useRef(null)

  const prontos = new Set(salvos.map(item => item.id))
  const lista = aba === 'salvos' ? salvos : recordings

  // silencioso: atualização de fundo não pisca a lista nem mostra erro de rede.
  async function load({ silent = false } = {}) {
    if (!silent) { setLoading(true); setError('') }
    try {
      const body = await lerResposta(await fetch(`${API}/recordings?${new URLSearchParams(query ? { query } : {})}`))
      setRecordings(Array.isArray(body) ? body : body.files || body.items || [])
    } catch (err) {
      if (!silent) setError(err.message || 'Não foi possível carregar as gravações.')
    } finally { if (!silent) setLoading(false) }
    carregarSalvos()
  }

  async function carregarSalvos() {
    try {
      const response = await fetch(`${API}/resumos`)
      if (response.ok) setSalvos(await response.json())
    } catch { /* biblioteca é complemento: falhar aqui não atrapalha a lista */ }
  }

  useEffect(() => { load() }, [])

  // O intervalo é criado uma vez só, então precisa de um ref para não congelar
  // no `load` da primeira renderização (que carrega a busca vazia).
  const loadRef = useRef(load)
  loadRef.current = load

  useEffect(() => {
    const atualizar = () => {
      if (document.visibilityState === 'visible') loadRef.current({ silent: true })
    }
    const timer = setInterval(atualizar, POLL_MS)
    // Voltar ao app é o momento em que a gravação nova costuma estar esperando.
    document.addEventListener('visibilitychange', atualizar)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', atualizar)
    }
  }, [])

  useEffect(() => {
    fetch(`${API}/models`)
      .then(response => response.json())
      .then(body => {
        const list = body.models || []
        const saved = localStorage.getItem('modelo')
        setModels(list)
        setModel(list.includes(saved) ? saved : body.default || '')
      })
      .catch(() => {})
  }, [])

  function chooseModel(value) {
    setModel(value)
    localStorage.setItem('modelo', value)
  }
  useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [chat, asking])

  async function process(recording, forcar = false) {
    setSelected(recording.id); setResult(null); setError(''); setChat([]); setQuestion(''); setSlide(0)
    try {
      const body = await lerResposta(await fetch(`${API}/recordings/${recording.id}/process`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ modelo: model, forcar }),
      }))
      setResult({ ...body, recordingId: recording.id, name: recording.name || body.nome })
      setChat(body.historico || [])
      carregarSalvos()  // acabou de virar uma resumida; a aba precisa saber
    } catch (err) { setError(err.message || 'Não foi possível processar o áudio.') }
    finally { setSelected(null) }
  }

  async function ask() {
    const pergunta = question.trim()
    if (!pergunta || asking || !result) return
    setQuestion(''); setAsking(true); setError('')
    try {
      const body = await lerResposta(await fetch(`${API}/recordings/${result.recordingId}/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pergunta, transcricao: result.transcricao, historico: chat, modelo: model }),
      }))
      setChat(current => [...current, { pergunta, resposta: body.resposta }])
    } catch (err) {
      setError(err.message || 'Não foi possível responder agora.')
      setQuestion(pergunta)
    } finally { setAsking(false) }
  }

  return <main>
    <div className="orb orb-one" /><div className="orb orb-two" />

    <header className="intro">
      <span className="intro-mark"><Sparkle /></span>
      <h1>resumos <em> da Lu</em></h1>
      <p>Escolha uma gravação, leia o resumo e pergunte o que quiser sobre a conversa.</p>
    </header>

    <section id="gravacoes" className="workspace">
      <div className="workspace-head">
        <div>
          <p className="section-label">BIBLIOTECA</p>
          <h2>Suas gravações</h2>
          <div className="abas">
            <button className={aba === 'plaud' ? 'ativa' : ''} onClick={() => setAba('plaud')}>Da Plaud</button>
            <button className={aba === 'salvos' ? 'ativa' : ''} onClick={() => setAba('salvos')}>
              Resumidas {salvos.length > 0 && <i>{salvos.length}</i>}
            </button>
          </div>
        </div>
        <div className="head-tools">
          {models.length > 1 && <label className="model-picker">
            <span>modelo</span>
            <select value={model} onChange={e => chooseModel(e.target.value)}>
              {models.map(item => <option key={item} value={item}>{item.replace('gemini-', '')}</option>)}
            </select>
          </label>}
          <div className="search">
            <input value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => e.key === 'Enter' && load()} placeholder="Encontre uma conversa" />
            <button className="search-button" onClick={load} aria-label="Buscar">⌕</button>
          </div>
        </div>
      </div>
      {error && <p className="error">{error}</p>}

      <section className="grid">
        <div className="panel recordings-panel">
          {loading && aba === 'plaud' ? <div className="loading"><span /><p>Buscando suas conversas…</p></div>
            : lista.length === 0 ? <div className="empty"><span>◌</span>
              <p>{aba === 'salvos' ? 'Nenhum resumo guardado ainda.' : 'Nenhuma gravação por aqui ainda.'}</p>
            </div>
              : lista.map((item, index) => <article className="recording" style={{ animationDelay: `${index * 45}ms` }} key={item.id}>
                <button className="play" aria-label="Abrir gravação" disabled={selected === item.id} onClick={() => process(item)}><Play /></button>
                <div className="recording-info">
                  <strong>{item.name || 'Sem título'}</strong>
                  <span>
                    {date(item.created_at || item.start_at)} <b>·</b> {Math.max(1, Math.round((item.duration || 0) / 60000))} min
                    {prontos.has(item.id) && <em className="tag-pronto">resumida</em>}
                  </span>
                </div>
                <button className="summarize" disabled={selected === item.id} onClick={() => process(item)}>
                  {selected === item.id ? <><i className="button-loader" /> lendo</>
                    : prontos.has(item.id) ? <>abrir <span>→</span></> : <>resumir <span>→</span></>}
                </button>
              </article>)}
        </div>

        <div className={`panel result ${result ? 'has-result' : ''}`}>
          {selected ? <div className="processing">
            <div className="thinking"><span /><span /><span /></div>
            <p className="section-label">OUVINDO</p>
            <h2>Já volto com o resumo.</h2>
            <p>Isso leva um instante.</p>
          </div> : result ? <>
            <div className="result-title">
              <div><p className="section-label">RESUMO</p><h2>{result.name || 'Sua conversa'}</h2></div>
              <div className="result-actions">
                <button onClick={() => window.print()} title="Salvar como PDF">baixar PDF</button>
                <button onClick={() => process({ id: result.recordingId, name: result.name }, true)} title="Descarta o material salvo e gera de novo">refazer</button>
              </div>
            </div>
            <div className="summary"><p>{result.resumo}</p></div>
            <div className="insights">
              {result.pontos_principais?.length > 0 && <div><h3>Cai na prova</h3><ul>{result.pontos_principais.map((point, i) => <li key={i}>{point}</li>)}</ul></div>}
              {result.acoes?.length > 0 && <div><h3>Recados e prazos</h3><ul className="actions">{result.acoes.map((action, i) => <li key={i}>{action}</li>)}</ul></div>}
            </div>
            {result.slides?.length > 0 && <Slides slides={result.slides} index={slide} onIndex={setSlide} />}
            {result.estudo && <div className="study">
              <h3>Material de estudo</h3>
              <div className="markdown"><Markdown remarkPlugins={[remarkGfm]}>{result.estudo}</Markdown></div>
            </div>}
            <details><summary>Ver transcrição completa <span>↓</span></summary><pre>{result.transcricao}</pre></details>

            <div className="ask">
              <h3>Perguntar sobre esta conversa</h3>
              {chat.length === 0 && !asking && <p className="ask-hint">Ex.: “o que ficou combinado sobre a viagem?”, “ela falou de datas?”</p>}
              {chat.length > 0 && <div className="chat">
                {chat.map((turn, i) => <div className="turn" key={i}>
                  <p className="bubble question">{turn.pergunta}</p>
                  <div className="bubble answer markdown"><Markdown remarkPlugins={[remarkGfm]}>{turn.resposta}</Markdown></div>
                </div>)}
              </div>}
              {asking && <p className="bubble answer pending"><i className="button-loader" /> procurando na transcrição…</p>}
              <div className="ask-box">
                <input value={question} onChange={e => setQuestion(e.target.value)} onKeyDown={e => e.key === 'Enter' && ask()} placeholder="Faça uma pergunta sobre o que foi dito" disabled={asking} />
                <button onClick={ask} disabled={asking || !question.trim()}>perguntar</button>
              </div>
              <span ref={chatEnd} />
            </div>
          </> : <div className="welcome">
            <span className="welcome-mark"><Sparkle /></span>
            <h2>Escolha uma conversa.</h2>
            <p>O resumo aparece aqui, e depois você pode perguntar qualquer coisa sobre ela.</p>
          </div>}
        </div>
      </section>
    </section>
  </main>
}
createRoot(document.getElementById('root')).render(<StrictMode><App /></StrictMode>)

if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}))
}
