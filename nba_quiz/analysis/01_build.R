library(tidyverse)
library(jsonlite)

dir.create("data/built", showWarnings = FALSE)

main <- read_csv("data/main_study_data.csv", show_col_types = FALSE)
pool <- read_csv("data/pretest_pool_data.csv", show_col_types = FALSE)

responses <- main %>%
  filter(!is.na(responses_json)) %>%
  transmute(pid, log = map(responses_json, fromJSON)) %>%
  unnest(log) %>%
  mutate(participant_distance = abs(answer - correct))

pool_distance <- pool %>%
  left_join(distinct(responses, game_id, correct), by = "game_id") %>%
  transmute(game_id, pool_distance = abs(answer - correct))

responses <- responses %>%
  left_join(pool_distance, by = "game_id", relationship = "many-to-many") %>%
  group_by(pid, round, game_id, answer, correct, outcome, participant_distance) %>%
  summarise(exp_win = mean(participant_distance < pool_distance), .groups = "drop")

rounds <- responses %>%
  group_by(pid, round) %>%
  summarise(n = n(), wins = sum(outcome == "won"), losses = sum(outcome == "lost"), exp_score = sum(exp_win), .groups = "drop") %>%
  pivot_wider(names_from = round, values_from = c(n, wins, losses, exp_score))

offered <- responses %>%
  filter(round == "r1") %>%
  inner_join(select(main, pid, review_option_a_game_id, review_option_b_game_id), by = "pid") %>%
  mutate(slot = case_when(game_id == review_option_a_game_id ~ "a", game_id == review_option_b_game_id ~ "b")) %>%
  filter(!is.na(slot)) %>%
  select(pid, slot, outcome, participant_distance, exp_win) %>%
  pivot_wider(names_from = slot, values_from = c(outcome, participant_distance, exp_win))

outcome_label <- c(won = "win", lost = "loss")
helpful_label <- c("Perform better after reviewing a win" = "win",
                   "Perform better after reviewing a loss" = "loss",
                   "No difference" = "no difference")

participants <- main %>%
  select(-responses_json) %>%
  left_join(rounds, by = "pid") %>%
  left_join(offered, by = "pid") %>%
  mutate(
    across(starts_with(c("n_", "wins_", "losses_")), \(x) replace_na(x, 0L)),
    passed_attention = att_check == "Somewhat disagree",
    passed_comprehension = !is.na(comprehension_completed_utc),
    completed_r1 = n_r1 == 5,
    win_and_loss_r1 = wins_r1 >= 1 & losses_r1 >= 1,
    completed_r2 = n_r2 == 5,
    closing_questions = !is.na(nba_fan_level) & !is.na(cheating),
    no_lookup = str_to_lower(str_trim(replace_na(cheating, ""))) != "yes",
    in_sample = passed_attention & entered_app & passed_comprehension & completed_r1 & win_and_loss_r1 &
      completed_r2 & closing_questions & no_lookup,
    review_assignment = unname(outcome_label[case_when(assigned_review_game_id == review_option_a_game_id ~ outcome_a,
                                                       assigned_review_game_id == review_option_b_game_id ~ outcome_b)]),
    review_pref = case_when(is.na(review_choice_game_id) ~ "indifferent",
                            review_choice_game_id == review_option_a_game_id ~ unname(outcome_label[outcome_a]),
                            review_choice_game_id == review_option_b_game_id ~ unname(outcome_label[outcome_b])),
    helpful = unname(helpful_label[review_self_report]),
    exp_score_dif = exp_score_r2 - exp_score_r1,
    assigned_loss = as.integer(review_assignment == "loss"),
    pref_win = as.integer(review_pref == "win"),
    pref_loss = as.integer(review_pref == "loss")
  )

write_csv(participants, "data/built/participant_level_data.csv", na = "")
write_csv(semi_join(responses, filter(participants, in_sample), by = "pid"), "data/built/response_level_data.csv", na = "")

cat("Analysis sample:", sum(participants$in_sample), "\n")
