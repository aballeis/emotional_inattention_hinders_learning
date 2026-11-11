# How Emotional Inattention Hinders Learning

Research code, data, and materials for Balleisen, Bolte, Golman, Hue, and Loewenstein, "How Emotional Inattention Hinders Learning." The paper has three components: Chess.com observational data (to be added), a survey of chess players (`chess_survey/`), and an online NBA prediction experiment (`nba_quiz/`). Each component is self-contained; run its scripts from the component folder, in order. Participant IDs in all data files are random IDs.

```
nba_quiz/                       NBA prediction study (preregistered)
  data/
    main_study_data.csv         one row per participant who started the study: survey answers, study-app
                                records, and the quiz answers of both rounds (responses_json)
    pretest_pool_data.csv       answers of the pretest participants who served as opponents
    llm_labels_<model>.csv      LLM labels of the stated reasons for the review choice
  analysis/                     run from nba_quiz/, in order
    01_build.R                  -> data/built/participant_level_data.csv, response_level_data.csv
    02_tables.R                 Tables: effect of the assigned review; sample flow
    03_figures.R                Figures: review preferences; stated reasons; confidence
    numbers.R                   -> tables_and_charts/numbers_check.md (every statistic in the text)
  interface/                    the study app (Flask)
  tables_and_charts/            generated tables and figures
chess_survey/                   survey of chess players (preregistered)
  data/
    survey_data.csv             survey responses
    llm_labels_<model>.csv      LLM labels of the open-ended answers
  analysis/                     run from chess_survey/, in order
    01_build.R                  -> data/built/survey_sample.csv (preregistered sample)
    02_tables.R                 Table: answers by Chess.com rating
    03_figures.R                Figure: survey answers
    numbers.R                   -> tables_and_charts/numbers_check.md
  tables_and_charts/
open_ended_llm.py               labels the open-ended answers of both studies through the Anthropic API
```

Output files are named after the paper's LaTeX files. When the scripts run inside a folder that also contains the paper (`../../paper`), they copy each table and figure there as well; otherwise they only write to `tables_and_charts/`.

## Requirements

Tested with these versions:

- **R 4.5.2**, with packages tidyverse 2.0.0, jsonlite 2.0.0, sandwich 3.1.1, patchwork 1.3.2
- **Python 3.14**, only for the study app (Flask) and to relabel the open-ended answers (`open_ended_llm.py`: pandas and the anthropic SDK; needs an Anthropic API key)

```
install.packages(c("tidyverse", "jsonlite", "sandwich", "patchwork"))
```

## Reproducing the results

```
cd nba_quiz
Rscript analysis/01_build.R
Rscript analysis/02_tables.R
Rscript analysis/03_figures.R
Rscript analysis/numbers.R

cd ../chess_survey
Rscript analysis/01_build.R
Rscript analysis/02_tables.R
Rscript analysis/03_figures.R
Rscript analysis/numbers.R
```

The LLM labels used in the paper are saved in the `data/` folders, so reproducing the results needs no API key. `python open_ended_llm.py --rerun` labels the answers again (Claude Fable 5.1 accepts no temperature setting, so a rerun can change a few labels).

## License and citation

Released under the MIT License (`LICENSE`). Citation information is in `CITATION.cff`.

## The NBA study app

To try the study locally, double-click `nba_quiz/interface/run_interface_locally.bat` (Windows), or run `python app.py` in `nba_quiz/interface/` and open http://127.0.0.1:8080. Responses are saved to `nba_quiz/interface/data/`. Opponents are drawn from `nba_quiz/data/pretest_pool_data.csv`. The quiz games are in `nba_games_data.js`, drawn with `python build_nba_games.py --season 2025-26 --n 5 --seed 1234 --out nba_games_data.js` (needs `pip install -r requirements.txt`). In the study, the app was embedded in a Qualtrics survey and hosted on a web server with persistent storage.
