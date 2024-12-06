# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import inspect
import os
import warnings
from collections import defaultdict
from dataclasses import FrozenInstanceError, replace
from typing import Any, Callable, Optional, Union

import pandas as pd
import torch
import torch.nn as nn
from accelerate import PartialState
from accelerate.utils import gather_object
from datasets import Dataset
from transformers import (
    BaseImageProcessor,
    DataCollator,
    FeatureExtractionMixin,
    PreTrainedModel,
    PreTrainedTokenizerBase,
    ProcessorMixin,
    Trainer,
    is_wandb_available,
)
from transformers.trainer_callback import TrainerCallback
from transformers.trainer_pt_utils import nested_detach
from transformers.trainer_utils import EvalPrediction
from transformers.utils import is_peft_available
from transformers.utils.deprecation import deprecate_kwarg

from ..data_utils import maybe_apply_chat_template
from ..models.modeling_cloud_head import CLoudRewardModel, CLoudRewardModelConfig
from .reward_config import RewardConfig
from .utils import (
    RewardDataCollatorWithPadding,
    compute_accuracy,
    decode_and_strip_padding,
    generate_model_card,
    print_rich_table,
)


if is_peft_available():
    from peft import PeftModel, get_peft_model, prepare_model_for_kbit_training

if is_wandb_available():
    import wandb


def _tokenize(
    batch: dict[str, list[Any]], tokenizer: "PreTrainedTokenizerBase", feedback_method: str
) -> dict[str, list[Any]]:
    """Tokenize a batch from a reward modelling dataset."""
    new_examples = {
        "input_ids_chosen": [],
        "attention_mask_chosen": [],
        "input_ids_rejected": [],
        "attention_mask_rejected": [],
    }
    for chosen, rejected in zip(batch["chosen"], batch["rejected"]):
        tokenized_chosen = tokenizer(chosen)
        tokenized_rejected = tokenizer(rejected)
        new_examples["input_ids_chosen"].append(tokenized_chosen["input_ids"])
        new_examples["attention_mask_chosen"].append(tokenized_chosen["attention_mask"])
        new_examples["input_ids_rejected"].append(tokenized_rejected["input_ids"])
        new_examples["attention_mask_rejected"].append(tokenized_rejected["attention_mask"])

        # Add feedback tokenization for CLoud teacher forcing
        if feedback_method == "teacher":
            if "chosen_feedback" in batch and "rejected_feedback" in batch:
                chosen_feedback = (
                    batch["chosen_feedback"][0]
                    if isinstance(batch["chosen_feedback"][0], list)
                    else batch["chosen_feedback"]
                )
                rejected_feedback = (
                    batch["rejected_feedback"][0]
                    if isinstance(batch["rejected_feedback"][0], list)
                    else batch["rejected_feedback"]
                )

                tokenized_chosen_feedback = tokenizer(chosen_feedback)
                tokenized_rejected_feedback = tokenizer(rejected_feedback)

                new_examples["chosen_lm_labels"].append(tokenized_chosen_feedback["input_ids"])
                new_examples["rejected_lm_labels"].append(tokenized_rejected_feedback["input_ids"])

    return new_examples


class RewardTrainer(Trainer):
    _tag_names = ["trl", "reward-trainer"]

    @deprecate_kwarg(
        "tokenizer", "0.15.0", "processing_class", warn_if_greater_or_equal_version=True, raise_if_both_names=True
    )
    def __init__(
        self,
        model: Optional[Union[PreTrainedModel, nn.Module]] = None,
        args: Optional[RewardConfig] = None,
        data_collator: Optional[DataCollator] = None,
        train_dataset: Optional[Dataset] = None,
        eval_dataset: Optional[Union[Dataset, dict[str, Dataset]]] = None,
        processing_class: Optional[
            Union[PreTrainedTokenizerBase, BaseImageProcessor, FeatureExtractionMixin, ProcessorMixin]
        ] = None,
        model_init: Optional[Callable[[], PreTrainedModel]] = None,
        compute_metrics: Optional[Callable[[EvalPrediction], dict]] = None,
        callbacks: Optional[list[TrainerCallback]] = None,
        optimizers: tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR] = (
            None,
            None,
        ),
        preprocess_logits_for_metrics: Optional[Callable[[torch.Tensor, torch.Tensor], torch.Tensor]] = None,
        max_length: Optional[int] = None,
        peft_config: Optional[dict] = None,
    ):
        """
        Initialize RewardTrainer.

        Args:
            model (`transformers.PreTrainedModel`):
                The model to train, preferably an `AutoModelForSequenceClassification`.
            args (`RewardConfig`):
                The arguments to use for training.
            data_collator (`transformers.DataCollator`):
                The data collator to use for training. If None is specified, the default data collator (`RewardDataCollatorWithPadding`) will be used
                which will pad the sequences to the maximum length of the sequences in the batch, given a dataset of paired sequences.
            train_dataset (`datasets.Dataset`):
                The dataset to use for training.
            eval_dataset (`datasets.Dataset`):
                The dataset to use for evaluation.
            processing_class (`PreTrainedTokenizerBase` or `BaseImageProcessor` or `FeatureExtractionMixin` or `ProcessorMixin`, *optional*):
                Processing class used to process the data. If provided, will be used to automatically process the inputs
                for the model, and it will be saved along the model to make it easier to rerun an interrupted training or
                reuse the fine-tuned model.
            model_init (`Callable[[], transformers.PreTrainedModel]`):
                The model initializer to use for training. If None is specified, the default model initializer will be used.
            compute_metrics (`Callable[[transformers.EvalPrediction], dict]`, *optional* defaults to `compute_accuracy`):
                The metrics to use for evaluation. If no metrics are specified, the default metric (`compute_accuracy`) will be used.
            callbacks (`list[transformers.TrainerCallback]`):
                The callbacks to use for training.
            optimizers (`tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR]`):
                The optimizer and scheduler to use for training.
            preprocess_logits_for_metrics (`Callable[[torch.Tensor, torch.Tensor], torch.Tensor]`):
                The function to use to preprocess the logits before computing the metrics.
            peft_config (`dict`, defaults to `None`):
                The PEFT configuration to use for training. If you pass a PEFT configuration, the model will be wrapped in a PEFT model.
        """
        if max_length is not None and args.max_length is not None:
            raise ValueError(
                "You cannot specify both `max_length` and `args.max_length`. Please use the `RewardConfig` to set `max_length` once."
            )
        if not is_peft_available() and peft_config is not None:
            raise ValueError(
                "PEFT is not installed and you passed a `peft_config` in the trainer's kwargs, please install it to use the PEFT models"
            )
        elif is_peft_available() and peft_config is not None:
            if not isinstance(model, PeftModel):
                if getattr(model, "is_loaded_in_8bit", False) or getattr(model, "is_quantized", False):
                    _supports_gc_kwargs = "gradient_checkpointing_kwargs" in list(
                        inspect.signature(prepare_model_for_kbit_training).parameters
                    )

                    prepare_model_kwargs = {"use_gradient_checkpointing": args.gradient_checkpointing}

                    if not _supports_gc_kwargs and args.gradient_checkpointing_kwargs is not None:
                        warnings.warn(
                            "You passed `gradient_checkpointing_kwargs` in the trainer's kwargs, but your peft version does not support it. "
                            "please update to the latest version of peft to use `gradient_checkpointing_kwargs`.",
                            UserWarning,
                        )
                    elif _supports_gc_kwargs and args.gradient_checkpointing_kwargs is not None:
                        prepare_model_kwargs["gradient_checkpointing_kwargs"] = args.gradient_checkpointing_kwargs

                    model = prepare_model_for_kbit_training(model, **prepare_model_kwargs)

                model = get_peft_model(model, peft_config)

        if compute_metrics is None:
            compute_metrics = compute_accuracy

        if data_collator is None:
            if processing_class is None:
                raise ValueError(
                    "A processing_class must be specified when using the default RewardDataCollatorWithPadding"
                )
            if max_length is None:
                max_length = 512 if args.max_length is None else args.max_length

            data_collator = RewardDataCollatorWithPadding(processing_class)

            if args.remove_unused_columns:
                try:  # for bc before https://github.com/huggingface/transformers/pull/25435
                    args.remove_unused_columns = False
                except FrozenInstanceError:
                    args = replace(args, remove_unused_columns=False)
                # warn users
                warnings.warn(
                    "When using RewardDataCollatorWithPadding, you should set `remove_unused_columns=False` in your RewardConfig"
                    " we have set it for you, but you should do it yourself in the future.",
                    UserWarning,
                )

            self.use_reward_data_collator = True
        else:
            self.use_reward_data_collator = False

        # The trainer estimates the number of FLOPs (floating-point operations) using the number of elements in the
        # input tensor associated with the key "input_ids". However, in Reward, the sampled data does not include the
        # "input_ids" key. Instead, the available keys are "input_ids_chosen" and "input_ids_rejected". As a result,
        # the trainer issues the warning: "Could not estimate the number of tokens of the input, floating-point
        # operations will not be computed." To suppress this warning, we set the "estimate_tokens" key in the model's
        # "warnings_issued" dictionary to True. This acts as a flag to indicate that the warning has already been
        # issued.
        model.warnings_issued["estimate_tokens"] = True

        if "input_ids_chosen" not in train_dataset.column_names:
            with PartialState().local_main_process_first():
                fn_kwargs = {"tokenizer": processing_class, "feedback_method": args.feedback_method}
                train_dataset = train_dataset.map(maybe_apply_chat_template, fn_kwargs={"tokenizer": processing_class})
                train_dataset = train_dataset.map(
                    _tokenize,
                    batched=True,
                    fn_kwargs=fn_kwargs,
                    num_proc=args.dataset_num_proc,
                )
                # This filter is important because otherwise you get samples that exceed the model's context length and
                # get truncated => noisy signal the chosen/rejected label gets lost. The downside is that the
                # user might get surprised if N samples are missing from training.
                train_dataset = train_dataset.filter(
                    lambda x: len(x["input_ids_chosen"]) <= max_length and len(x["input_ids_rejected"]) <= max_length,
                    num_proc=args.dataset_num_proc,
                )
                if eval_dataset is not None:
                    eval_dataset = eval_dataset.map(
                        maybe_apply_chat_template, fn_kwargs={"tokenizer": processing_class}
                    )
                    eval_dataset = eval_dataset.map(
                        _tokenize,
                        fn_kwargs=fn_kwargs,
                        batched=True,
                        num_proc=args.dataset_num_proc,
                    )
                    # This filter is important because otherwise you get samples that exceed the model's context length and
                    # get truncated => noisy signal the chosen/rejected label gets lost. The downside is that the
                    # user might get surprised if N samples are missing from training.
                    eval_dataset = eval_dataset.filter(
                        lambda x: len(x["input_ids_chosen"]) <= max_length
                        and len(x["input_ids_rejected"]) <= max_length,
                        num_proc=args.dataset_num_proc,
                    )

        self.feedback_method = args.feedback_method
        if self.feedback_method is not None:
            self.lm_loss = nn.CrossEntropyLoss(ignore_index=-100)
            if model is None and args.model_name_or_path is not None:
                model = CLoudRewardModel(
                    CLoudRewardModelConfig(
                        base_model_name_or_path=args.model_name_or_path,
                        feedback_method=self.feedback_method,
                    )
                )

        super().__init__(
            model=model,
            args=args,
            data_collator=data_collator,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            processing_class=processing_class,
            model_init=model_init,
            compute_metrics=compute_metrics,
            callbacks=callbacks,
            optimizers=optimizers,
            preprocess_logits_for_metrics=preprocess_logits_for_metrics,
        )

        # Add tags for models that have been loaded with the correct transformers version
        if hasattr(self.model, "add_model_tags"):
            self.model.add_model_tags(self._tag_names)

    def compute_loss(
        self,
        model: Union[PreTrainedModel, nn.Module],
        inputs: dict[str, Union[torch.Tensor, Any]],
        return_outputs=False,
        num_items_in_batch=None,
    ) -> Union[torch.Tensor, tuple[torch.Tensor, dict[str, torch.Tensor]]]:
        # Get outputs for chosen and rejected responses
        chosen_outputs = model(
            input_ids=inputs["input_ids_chosen"],
            attention_mask=inputs["attention_mask_chosen"],
            return_dict=True,
        )
        rejected_outputs = model(
            input_ids=inputs["input_ids_rejected"],
            attention_mask=inputs["attention_mask_rejected"],
            return_dict=True,
        )

        # The reward scores come from the reward head
        rewards_chosen = chosen_outputs["logits"]  # This is the reward score
        rewards_rejected = rejected_outputs["logits"]  # This is the reward score

        # Calculate reward loss
        if "margin" in inputs:
            reward_loss = -nn.functional.logsigmoid(rewards_chosen - rewards_rejected - inputs["margin"]).mean()
        else:
            reward_loss = -nn.functional.logsigmoid(rewards_chosen - rewards_rejected).mean()

        if self.args.center_rewards_coefficient is not None:
            reward_loss += self.args.center_rewards_coefficient * torch.mean((rewards_chosen + rewards_rejected) ** 2)

        # Add language modeling loss for teacher forcing
        if self.feedback_method == "teacher":
            # The language model logits come from the base model
            chosen_lm_loss = self.lm_loss(
                chosen_outputs["lm_logits"].view(-1, model.reward_base_model.config.vocab_size),
                inputs.get("chosen_lm_labels", inputs["input_ids_chosen"]).view(-1),
            )
            rejected_lm_loss = self.lm_loss(
                rejected_outputs["lm_logits"].view(-1, model.reward_base_model.config.vocab_size),
                inputs.get("rejected_lm_labels", inputs["input_ids_rejected"]).view(-1),
            )
            lm_loss = (chosen_lm_loss + rejected_lm_loss) / 2
            loss = reward_loss + self.args.lm_weight * lm_loss
        else:
            loss = reward_loss

        if return_outputs:
            return loss, {
                "rewards_chosen": rewards_chosen,
                "rewards_rejected": rewards_rejected,
                "chosen_logits": chosen_outputs["lm_logits"] if self.feedback_method == "teacher" else None,
                "rejected_logits": rejected_outputs["lm_logits"] if self.feedback_method == "teacher" else None,
            }
        return loss

    def prediction_step(
        self,
        model: Union[PreTrainedModel, nn.Module],
        inputs: dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[list[str]] = None,
    ) -> tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
        inputs = self._prepare_inputs(inputs)
        if ignore_keys is None:
            if hasattr(self.model, "config"):
                ignore_keys = getattr(self.model.config, "keys_to_ignore_at_inference", [])
            else:
                ignore_keys = []

        with torch.no_grad():
            loss, logits_dict = self.compute_loss(model, inputs, return_outputs=True)

        if prediction_loss_only:
            return (loss, None, None)

        loss = loss.detach()
        logits = tuple(v for k, v in logits_dict.items() if k not in ignore_keys)
        logits = nested_detach(logits)
        # Stack accepted against rejected, mean over logits
        # and softmax to get preferences between accepted and rejected to sum to 1
        logits = torch.stack(logits).mean(dim=2).softmax(dim=0).T

        labels = torch.zeros(logits.shape[0])
        labels = self._prepare_inputs(labels)

        return loss, logits, labels

    def evaluate(self, *args, **kwargs):
        num_print_samples = kwargs.pop("num_print_samples", 4)
        self.visualize_samples(num_print_samples)
        return super().evaluate(*args, **kwargs)

    def visualize_samples(self, num_print_samples: int):
        """
        Visualize the reward model logits prediction

        Args:
            num_print_samples (`int`, defaults to `4`):
                The number of samples to print. Set to `-1` to print all samples.
        """
        eval_dataloader = self.get_eval_dataloader()
        table = defaultdict(list)
        for _, inputs in enumerate(eval_dataloader):
            _, logits, _ = self.prediction_step(self.model, inputs, prediction_loss_only=False)
            chosen_text = decode_and_strip_padding(inputs["input_ids_chosen"], self.processing_class)
            rejected_text = decode_and_strip_padding(inputs["input_ids_rejected"], self.processing_class)
            table["chosen_text"].extend(gather_object(chosen_text))
            table["rejected_text"].extend(gather_object(rejected_text))
            table["logits"].extend(
                gather_object([[round(inner_item, 4) for inner_item in item] for item in logits.tolist()])
            )
            if num_print_samples >= 0 and len(table["chosen_text"]) >= num_print_samples:
                break
        df = pd.DataFrame(table)
        if self.accelerator.process_index == 0:
            print_rich_table(df[:num_print_samples])
            if "wandb" in self.args.report_to:
                import wandb

                if wandb.run is not None:
                    wandb.log({"completions": wandb.Table(dataframe=df)})

    def create_model_card(
        self,
        model_name: Optional[str] = None,
        dataset_name: Optional[str] = None,
        tags: Union[str, list[str], None] = None,
    ):
        """
        Creates a draft of a model card using the information available to the `Trainer`.

        Args:
            model_name (`str`, *optional*, defaults to `None`):
                The name of the model.
            dataset_name (`str`, *optional*, defaults to `None`):
                The name of the dataset used for training.
            tags (`str`, `list[str]` or `None`, *optional*, defaults to `None`):
                Tags to be associated with the model card.
        """
        if not self.is_world_process_zero():
            return

        if hasattr(self.model.config, "_name_or_path") and not os.path.isdir(self.model.config._name_or_path):
            base_model = self.model.config._name_or_path
        else:
            base_model = None

        tags = tags or []
        if isinstance(tags, str):
            tags = [tags]

        if hasattr(self.model.config, "unsloth_version"):
            tags.append("unsloth")

        model_card = generate_model_card(
            base_model=base_model,
            model_name=model_name,
            hub_model_id=self.hub_model_id,
            dataset_name=dataset_name,
            tags=tags,
            wandb_url=wandb.run.get_url() if is_wandb_available() and wandb.run is not None else None,
            trainer_name="Reward",
        )

        model_card.save(os.path.join(self.args.output_dir, "README.md"))
