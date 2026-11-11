"""
Labels the open-ended answers of both studies through the Anthropic API (Appendix: classifier prompts).
Not needed to reproduce the results: the saved labels are in nba_quiz/data/llm_labels_<model>.csv and
chess_survey/data/llm_labels_<model>.csv. Run the two 01_build.R scripts first.

    python open_ended_llm.py            # label only what has no saved labels yet
    python open_ended_llm.py --rerun    # call the API again and overwrite the saved labels

Main model: Claude Fable 5.1 (default settings); robustness: Claude Haiku 4.5 at temperature 0. One request per answer;
the model sees only the fixed prompt and the answer (for the NBA study, also the stated preference). Fable 5.1 accepts no
temperature, so a rerun can change a few labels. Needs the anthropic Python SDK (1.x) and ANTHROPIC_API_KEY.
"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
import pandas as pd

REPO = os.path.dirname(os.path.abspath(__file__))
MODELS = {"fable-5-1": dict(model="claude-fable-5-1"), "haiku-4-5": dict(model="claude-haiku-4-5", temperature=0)}
MAIN = "fable-5-1"

NBA_CATS = ["hedonic", "instrumental", "curiosity", "team", "indifference", "other"]
NBA_PROMPT = """You are labeling free-text survey responses. Participants in an experiment predicted the outcomes of five NBA games against an opponent, ending up with at least one win and one loss. They were then asked which of the two games, the win or the loss, they would prefer to review (or said they had no preference), knowing that they would predict the same five games again in a second round, and explained their choice.

Label each explanation with every category that applies:

- hedonic: wanting to feel good, relive or savor the win, or avoid the bad feeling of confronting the loss.
- instrumental: wanting to learn, understand a mistake, improve accuracy, or do better in the second round.
- curiosity: wanting to see how the game unfolded or how close the guess was, without a stated learning purpose.
- team: interest in the specific teams or matchup, fandom, or team knowledge.
- indifference: the choice does not matter to them, or no real reason.
- other: none of the above applies.

Also give a one-sentence explanation of your labels."""
NBA_USER = "Stated preference: {pref}\nExplanation: {text}"

SURVEY_CATS = ["learn", "unsure", "curious", "time", "avoid", "savor", "gametype"]
SURVEY_PROMPT = """You are labeling free-text survey responses. Chess players who play on Chess.com were asked: "When you finish a game on Chess.com, what factors determine whether you decide to review it?" Reviewing a game means going through it after it is finished with an analysis engine or Chess.com's Game Review tool.

Label each answer with every category that applies:

- learn: wants to learn, find mistakes, or improve.
- unsure: reviews when unsure what happened or where the game went wrong.
- curious: the game or a position was interesting, complicated, close, or hard-fought.
- time: time, being busy, or wanting to play the next game instead.
- avoid: skips games, typically losses, because of frustration, tilt, or self-reproach.
- savor: reviews to enjoy a good game or a brilliant move, or to confirm it was as good as it felt.
- gametype: depends on the time control, the opponent, the result being a draw, or whether the game was played in person.

Also give a one-sentence explanation of your labels."""
SURVEY_USER = "Answer: {text}"


def schema(cats):
    return {"type": "object", "additionalProperties": False, "required": cats + ["note"],
            "properties": {**{c: {"type": "boolean"} for c in cats}, "note": {"type": "string"}}}


def classify(client, spec, system, user, cats):
    oc = {"format": {"type": "json_schema", "schema": schema(cats)}}
    kw = dict(model=spec["model"], max_tokens=4000, system=system, output_config=oc, messages=[{"role": "user", "content": user}])
    if "temperature" in spec: kw["extra_body"] = {"temperature": spec["temperature"]}   # SDK 1.x dropped the keyword; Haiku 4.5 still honours it
    r = client.messages.create(**kw)
    if r.stop_reason == "refusal": return {**{c: -1 for c in cats}, "note": "REFUSED"}
    out = json.loads(next(b.text for b in r.content if b.type == "text"))
    return {**{c: int(out[c]) for c in cats}, "note": out["note"]}


def nba_input():
    d = pd.read_csv(os.path.join(REPO, "nba_quiz", "data", "built", "participant_level_data.csv"), low_memory=False)
    d = d[d.in_sample].sort_values("pid")
    return pd.DataFrame({"pid": d.pid, "pref": d.review_pref, "text": d.review_choice_reason.fillna("").astype(str).str.strip()}).reset_index(drop=True)


def survey_input():
    d = pd.read_csv(os.path.join(REPO, "chess_survey", "data", "built", "survey_sample.csv"))
    return pd.DataFrame({"response_id": d.response_id, "text": d.free_text_factors.fillna("").astype(str).str.strip()})


def run(name, spec, rows, system, user_fmt, cats, path, keys):
    if os.path.exists(path) and "--rerun" not in sys.argv:
        print(f"{os.path.basename(path)}: saved labels kept"); return
    import anthropic
    client = anthropic.Anthropic(max_retries=5); pref_names = {"win": "the question they won", "loss": "the question they lost", "indifferent": "no preference"}
    def one(r):
        user = user_fmt.format(text=r["text"] if r["text"] else "(no answer)", pref=pref_names.get(r.get("pref"), r.get("pref")))
        return {**{k: r[k] for k in keys}, "model": spec["model"], **classify(client, spec, system, user, cats)}
    with ThreadPoolExecutor(8) as ex: out = list(ex.map(one, rows.to_dict("records")))
    pd.DataFrame(out).to_csv(path, index=False); print(f"wrote {os.path.basename(path)}")


def main():
    nba, sv = nba_input(), survey_input()
    for name, spec in MODELS.items():
        run(name, spec, nba, NBA_PROMPT, NBA_USER, NBA_CATS, os.path.join(REPO, "nba_quiz", "data", f"llm_labels_{name}.csv"), ["pid", "pref"])
        run(name, spec, sv, SURVEY_PROMPT, SURVEY_USER, SURVEY_CATS, os.path.join(REPO, "chess_survey", "data", f"llm_labels_{name}.csv"), ["response_id"])


if __name__ == "__main__":
    main()
