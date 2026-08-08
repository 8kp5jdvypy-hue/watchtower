import './Grain.css'

// The one texture layer that separates "cyan-on-black SaaS" from
// "cinematic/expensive." Cheap: a single tiled SVG noise filter, no assets.
export default function Grain() {
  return <div className="film-grain" aria-hidden="true" />
}
