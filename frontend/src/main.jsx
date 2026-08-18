import { StrictMode, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './styles.css'

const API = import.meta.env.VITE_API_URL || 'http://localhost:5001/api'
const POLL_MS = 30 * 1000

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

// Traços guardados em fração da largura/altura, não em pixels: o mesmo desenho
// volta certo no iPad em pé, deitado ou na impressão.
const CORES = ['#292437', '#c0392b', '#2f7ec7', '#3f8f68', '#c58a00']
const PROPORCAO = 0.72   // altura da folha em relação à largura
const ESPESSURAS = [1.6, 3, 7]

function Anotacoes({ tracos, onMudar }) {
  const wrap = useRef(null)
  const canvas = useRef(null)
  const atual = useRef(null)
  const [cor, setCor] = useState(CORES[0])
  const [espessura, setEspessura] = useState(ESPESSURAS[1])
  const [borracha, setBorracha] = useState(false)
  // Na tela do iPad o dedo serve para rolar a página; quem desenha é a caneta.
  const [soCaneta, setSoCaneta] = useState(true)
  const [tamanho, setTamanho] = useState({ largura: 0, altura: 0 })

  useEffect(() => {
    const alvo = wrap.current
    if (!alvo) return
    const medir = () => setTamanho({ largura: alvo.clientWidth, altura: Math.round(alvo.clientWidth * PROPORCAO) })
    medir()
    const observer = new ResizeObserver(medir)
    observer.observe(alvo)
    return () => observer.disconnect()
  }, [])

  function desenharTraco(ctx, traco, largura, altura) {
    if (!traco.pontos.length) return
    ctx.strokeStyle = traco.cor
    ctx.lineWidth = traco.espessura
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'
    ctx.beginPath()
    traco.pontos.forEach(([x, y], i) => {
      const px = x * largura, py = y * altura
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py)
    })
    // Um toque isolado ainda deve deixar um ponto na folha.
    if (traco.pontos.length === 1) ctx.lineTo(traco.pontos[0][0] * largura + 0.1, traco.pontos[0][1] * altura)
    ctx.stroke()
  }

  useEffect(() => {
    const elemento = canvas.current
    const { largura, altura } = tamanho
    if (!elemento || !largura) return
    const dpr = window.devicePixelRatio || 1
    elemento.width = largura * dpr
    elemento.height = altura * dpr
    const ctx = elemento.getContext('2d')
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, largura, altura)
    tracos.forEach(traco => desenharTraco(ctx, traco, largura, altura))
  }, [tracos, tamanho])

  function posicao(evento) {
    const caixa = canvas.current.getBoundingClientRect()
    return [(evento.clientX - caixa.left) / caixa.width, (evento.clientY - caixa.top) / caixa.height]
  }

  // Apaga o traço inteiro que passa perto do ponto: mais previsível do que
  // tentar recortar a linha no meio.
  function apagarEm([x, y]) {
    const raio = 0.02
    const sobrou = tracos.filter(traco => !traco.pontos.some(([px, py]) =>
      Math.abs(px - x) < raio && Math.abs(py - y) * PROPORCAO < raio))
    if (sobrou.length !== tracos.length) onMudar(sobrou)
  }

  function comecar(evento) {
    if (soCaneta && evento.pointerType === 'touch') return
    evento.currentTarget.setPointerCapture(evento.pointerId)
    const ponto = posicao(evento)
    if (borracha) { atual.current = 'borracha'; apagarEm(ponto); return }
    atual.current = { cor, espessura, pontos: [ponto] }
  }

  function mover(evento) {
    if (!atual.current) return
    const ponto = posicao(evento)
    if (atual.current === 'borracha') return apagarEm(ponto)
    atual.current.pontos.push(ponto)
    // Desenha só o segmento novo: redesenhar tudo a cada movimento trava no iPad.
    const ctx = canvas.current.getContext('2d')
    const { largura, altura } = tamanho
    const pontos = atual.current.pontos
    const [ax, ay] = pontos[pontos.length - 2] || ponto
    ctx.strokeStyle = atual.current.cor
    ctx.lineWidth = atual.current.espessura
    ctx.lineCap = 'round'
    ctx.beginPath()
    ctx.moveTo(ax * largura, ay * altura)
    ctx.lineTo(ponto[0] * largura, ponto[1] * altura)
    ctx.stroke()
  }

  function terminar() {
    const traco = atual.current
    atual.current = null
    if (traco && traco !== 'borracha') onMudar([...tracos, traco])
  }

  return <div className="anotacoes">
    <div className="anotacoes-head">
      <h3>Suas anotações</h3>
      <div className="caneta">
        {CORES.map(item => <button key={item} className={`tinta ${!borracha && cor === item ? 'ativa' : ''}`}
          style={{ background: item }} onClick={() => { setCor(item); setBorracha(false) }} aria-label={`Cor ${item}`} />)}
        {ESPESSURAS.map(item => <button key={item} className={`ponta ${!borracha && espessura === item ? 'ativa' : ''}`}
          onClick={() => { setEspessura(item); setBorracha(false) }} aria-label={`Espessura ${item}`}>
          <i style={{ width: item * 2.4, height: item * 2.4 }} />
        </button>)}
        <button className={borracha ? 'ativa' : ''} onClick={() => setBorracha(!borracha)}>borracha</button>
        <button className={soCaneta ? 'ativa' : ''} onClick={() => setSoCaneta(!soCaneta)} title="Ignora o toque do dedo, para poder rolar a página">só caneta</button>
        <button onClick={() => tracos.length && onMudar(tracos.slice(0, -1))} disabled={!tracos.length}>desfazer</button>
        <button onClick={() => tracos.length && confirm('Apagar todas as anotações desta aula?') && onMudar([])} disabled={!tracos.length}>limpar</button>
      </div>
    </div>
    <div className="folha" ref={wrap}>
      <canvas
        ref={canvas}
        style={{ width: '100%', height: tamanho.altura || 1, touchAction: soCaneta ? 'auto' : 'none' }}
        onPointerDown={comecar} onPointerMove={mover} onPointerUp={terminar} onPointerCancel={terminar} onPointerLeave={terminar}
      />
    </div>
  </div>
}

function TituloEditavel({ nome, onSalvar }) {
  const [editando, setEditando] = useState(false)
  const [texto, setTexto] = useState(nome)
  const campo = useRef(null)

  useEffect(() => { setTexto(nome) }, [nome])

  // Nome de aula é longo: em vez de rolar dentro de uma linha só, o campo
  // quebra e cresce, mostrando o título inteiro enquanto ela digita.
  useEffect(() => {
    const elemento = campo.current
    if (!elemento) return
    elemento.style.height = 'auto'
    elemento.style.height = `${elemento.scrollHeight}px`
  }, [texto, editando])

  function confirmar() {
    const limpo = texto.trim()
    setEditando(false)
    if (limpo && limpo !== nome) onSalvar(limpo)
    else setTexto(nome)
  }

  if (!editando) return <h2 className="titulo-resumo">
    {nome || 'Sua conversa'}
    <button className="editar-nome" onClick={() => setEditando(true)} aria-label="Editar o nome do resumo">✎</button>
  </h2>

  return <textarea className="titulo-input" ref={campo} autoFocus rows={1} value={texto}
    onChange={e => setTexto(e.target.value.replace(/\n/g, ''))}
    onBlur={confirmar}
    onKeyDown={e => {
      if (e.key === 'Enter') { e.preventDefault(); confirmar() }
      if (e.key === 'Escape') { setTexto(nome); setEditando(false) }
    }} />
}

function NomeDoCartao({ item, onSalvar }) {
  const [editando, setEditando] = useState(false)
  const [texto, setTexto] = useState(item.name || '')

  useEffect(() => { setTexto(item.name || '') }, [item.name])

  function confirmar() {
    const limpo = texto.trim()
    setEditando(false)
    if (limpo && limpo !== item.name) onSalvar(item.id, limpo)
    else setTexto(item.name || '')
  }

  if (editando) return <input className="nome-input" autoFocus value={texto}
    onChange={e => setTexto(e.target.value)}
    onBlur={confirmar}
    onKeyDown={e => {
      if (e.key === 'Enter') confirmar()
      if (e.key === 'Escape') { setTexto(item.name || ''); setEditando(false) }
    }} />

  return <strong>
    {item.name || 'Sem título'}
    <button className="editar-item" onClick={() => setEditando(true)} title="Renomear" aria-label={`Renomear ${item.name || 'esta gravação'}`}>✎</button>
  </strong>
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
  const [anotacoes, setAnotacoes] = useState([])
  const [salvos, setSalvos] = useState([])
  const chatEnd = useRef(null)
  const salvarAnotacoes = useRef(null)

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

  // Abriu um resumo no celular: sobe até ele, senão o conteúdo nasce fora da tela.
  useEffect(() => {
    if (result && window.matchMedia('(max-width: 820px)').matches) {
      document.getElementById('gravacoes')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [result?.recordingId])

  async function process(recording, forcar = false) {
    setSelected(recording.id); setResult(null); setError(''); setChat([]); setQuestion(''); setSlide(0); setAnotacoes([])
    try {
      const body = await lerResposta(await fetch(`${API}/recordings/${recording.id}/process`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ modelo: model, forcar }),
      }))
      setResult({ ...body, recordingId: recording.id, name: body.nome || recording.name })
      setAnotacoes(Array.isArray(body.anotacoes) ? body.anotacoes : [])
      setChat(body.historico || [])
      carregarSalvos()  // acabou de virar uma resumida; a aba precisa saber
    } catch (err) { setError(err.message || 'Não foi possível processar o áudio.') }
    finally { setSelected(null) }
  }

  async function editar(mudancas) {
    return lerResposta(await fetch(`${API}/recordings/${result.recordingId}/resumo`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(mudancas),
    }))
  }

  // Renomeia da lista ou do resumo aberto: o nome é guardado por gravação,
  // mesmo que ela ainda não tenha sido resumida.
  async function renomear(recordingId, nome) {
    const trocar = lista => lista.map(item => item.id === recordingId ? { ...item, name: nome } : item)
    setRecordings(trocar); setSalvos(trocar)
    if (result?.recordingId === recordingId) setResult(current => ({ ...current, name: nome, nome }))
    setError('')
    try {
      await lerResposta(await fetch(`${API}/recordings/${recordingId}/resumo`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nome }),
      }))
    } catch (err) {
      setError(err.message || 'Não foi possível renomear.')
      load()
    }
  }

  // Cada traço salvo faria uma requisição; espera a caneta parar antes de gravar.
  function anotar(novos) {
    setAnotacoes(novos)
    clearTimeout(salvarAnotacoes.current)
    salvarAnotacoes.current = setTimeout(() => {
      editar({ anotacoes: novos }).catch(err => setError(err.message || 'Não foi possível salvar as anotações.'))
    }, 1200)
  }

  // Apaga só o material guardado: a gravação continua na Plaud e pode ser
  // resumida de novo.
  async function apagar(recording, { fechar = false } = {}) {
    const nome = recording.name || 'este resumo'
    if (!confirm(`Apagar o resumo de "${nome}"? As anotações e as perguntas vão junto.`)) return
    setError('')
    try {
      await lerResposta(await fetch(`${API}/recordings/${recording.id}/resumo`, { method: 'DELETE' }))
      setSalvos(current => current.filter(item => item.id !== recording.id))
      if (fechar || result?.recordingId === recording.id) { setResult(null); setChat([]); setAnotacoes([]) }
    } catch (err) {
      setError(err.message || 'Não foi possível apagar o resumo.')
      carregarSalvos()
    }
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

      {/* No celular os dois painéis empilham: com resultado aberto, ele toma a
          tela inteira e a lista some, como num app de verdade. */}
      <section className={`grid ${result || selected ? 'com-resultado' : ''}`}>
        <div className="panel recordings-panel">
          {loading && aba === 'plaud' ? <div className="loading"><span /><p>Buscando suas conversas…</p></div>
            : lista.length === 0 ? <div className="empty"><span>◌</span>
              <p>{aba === 'salvos' ? 'Nenhum resumo guardado ainda.' : 'Nenhuma gravação por aqui ainda.'}</p>
            </div>
              : lista.map((item, index) => <article className="recording" style={{ animationDelay: `${index * 45}ms` }} key={item.id}>
                <button className="play" aria-label="Abrir gravação" disabled={selected === item.id} onClick={() => process(item)}><Play /></button>
                <div className="recording-info">
                  <NomeDoCartao item={item} onSalvar={renomear} />
                  <span>
                    {date(item.created_at || item.start_at)} <b>·</b> {Math.max(1, Math.round((item.duration || 0) / 60000))} min
                    {prontos.has(item.id) && <em className="tag-pronto">resumida</em>}
                  </span>
                </div>
                <button className="summarize" disabled={selected === item.id} onClick={() => process(item)}>
                  {selected === item.id ? <><i className="button-loader" /> lendo</>
                    : prontos.has(item.id) ? <>abrir <span>→</span></> : <>resumir <span>→</span></>}
                </button>
                {prontos.has(item.id) && <button className="apagar-item" onClick={() => apagar(item)} aria-label={`Apagar o resumo de ${item.name || 'sem título'}`} title="Apagar este resumo">✕</button>}
              </article>)}
        </div>

        <div className={`panel result ${result ? 'has-result' : ''}`}>
          {selected ? <div className="processing">
            <div className="thinking"><span /><span /><span /></div>
            <p className="section-label">OUVINDO</p>
            <h2>Já volto com o resumo.</h2>
            <p>Isso leva um instante.</p>
          </div> : result ? <>
            <button className="voltar" onClick={() => { setResult(null); setChat([]) }}>← gravações</button>
            <div className="result-title">
              <div><p className="section-label">RESUMO</p><TituloEditavel nome={result.name || 'Sua conversa'} onSalvar={nome => renomear(result.recordingId, nome)} /></div>
              <div className="result-actions">
                <button onClick={() => window.print()} title="Salvar como PDF">baixar PDF</button>
                <button onClick={() => process({ id: result.recordingId, name: result.name }, true)} title="Descarta o material salvo e gera de novo">refazer</button>
                {prontos.has(result.recordingId) && <button className="apagar" onClick={() => apagar({ id: result.recordingId, name: result.name }, { fechar: true })} title="Tira da biblioteca; a gravação continua na Plaud">apagar</button>}
              </div>
            </div>
            <div className="summary"><p>{result.resumo}</p></div>
            {result.acoes?.length > 0 && <div className="insights">
              <div><h3>Recados e prazos</h3><ul className="actions">{result.acoes.map((action, i) => <li key={i}>{action}</li>)}</ul></div>
            </div>}
            {result.slides?.length > 0 && <Slides slides={result.slides} index={slide} onIndex={setSlide} />}
            {result.estudo && <div className="study">
              <h3>Material de estudo</h3>
              <div className="markdown"><Markdown remarkPlugins={[remarkGfm]}>{result.estudo}</Markdown></div>
            </div>}
            <Anotacoes tracos={anotacoes} onMudar={anotar} />
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
