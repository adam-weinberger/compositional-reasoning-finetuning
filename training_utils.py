from typing import Dict, List
import os
import re
import numpy as np
import pandas as pd

import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
from transformers import T5Tokenizer, TFT5ForConditionalGeneration

import json


def qa_split(examples: List[Dict[str, str]], triple=False) -> List[str]:
    '''Splits the examples into questions and answers.
    
    Adapts structure of output of load_FinetuningData for tokenizer.
    '''
    if triple:
        questions = [example["prompt"] for example in examples]
        targets = [example["target"] for example in examples]
        answers = [example["answer"] for example in examples]
        return questions, targets, answers
    else:
        questions = [example["prompt"] for example in examples]
        answers = [example["target"] for example in examples]
        return questions, answers


def preprocess_data(text_pairs, tokenizer, model, max_length=512):
    prompt_text = [prompt for prompt, target in text_pairs]
    prompt_encoded = tokenizer.batch_encode_plus(
        prompt_text,
        max_length=max_length,
        padding='max_length',
        truncation=True,
        return_attention_mask=True,
        return_tensors='tf'
    )

    prompt_input_ids = np.array(prompt_encoded["input_ids"], dtype="int32")
    prompt_attention_masks = np.array(prompt_encoded["attention_mask"], dtype="int32")

    target_text = [target for orig, target in text_pairs]
    target_encoded = tokenizer.batch_encode_plus(
        target_text,
        max_length=max_length,
        padding='max_length',
        truncation=True,
        return_tensors='tf'
    )

    label_ids = np.array(target_encoded['input_ids'])
    decoder_input_ids = model._shift_right(label_ids)

    return [prompt_input_ids, prompt_attention_masks, decoder_input_ids], label_ids


class MultihopQADataGenerator(tf.keras.utils.Sequence):

    def __init__(self,
                 tokenizer,
                 model,
                 n_examples,
                 data_filename,
                 max_length=512,
                 batch_size=16,
                 shuffle=True):

        self.tokenizer = tokenizer
        self.model = model
        self.n_examples = n_examples
        self.data_filename = data_filename
        self.max_length = max_length
        self.batch_size = batch_size
        self.shuffle = shuffle

        # Initialize row order, call on_epoch_end to shuffle row indices
        self.row_order = np.arange(1, self.n_examples+1)
        self.on_epoch_end()

    def __len__(self):
        # Return the number of batches in the full dataset
        return self.n_examples // self.batch_size

    def __getitem__(self, idx):
        batch_start = idx * self.batch_size
        batch_end = (idx + 1) * self.batch_size

        # Indices to skip are the ones in the shuffled row_order before and
        # after the chunk we'll use for this batch
        batch_idx_skip = self.row_order[:batch_start] + self.row_order[batch_end:]
        df = pd.read_json(self.data_filename)
        df = df.iloc[batch_start : batch_end]

        text_pairs = df[['prompt', 'target']].values.astype(str).tolist()

        batch_data = preprocess_data(
            text_pairs,
            self.tokenizer,
            self.model,
            self.max_length
        )

        return batch_data

    def __call__(self):
        for i in range(self.__len__()):
            yield self.__getitem__(i)

            if i == self.__len__()-1:
                self.on_epoch_end()

    def on_epoch_end(self):
        if self.shuffle:
            self.row_order = list(np.random.permutation(self.row_order))


def build_t5_training_wrapper_model(t5_model, max_length):
    input_ids = layers.Input(shape=(max_length), dtype=tf.int32, name='input_ids')
    attention_mask = layers.Input(shape=(max_length), dtype=tf.int32, name='attention_mask')
    decoder_input_ids = layers.Input(shape=(max_length), dtype=tf.int32, name='labels')

    t5_logits = t5_model(input_ids, attention_mask=attention_mask, decoder_input_ids=decoder_input_ids)[0]

    model = tf.keras.models.Model(inputs=[input_ids, attention_mask, decoder_input_ids],
                                  outputs=[t5_logits])
    model.compile(optimizer=tf.keras.optimizers.Adam(),
                  loss=tf.losses.SparseCategoricalCrossentropy(from_logits=True),
                  metrics=['accuracy'])

    return model


def finetune_self_ask(model_name, train_file, valid_file, checkpoint_filepath, max_length = 128, batch_size = 16, epochs = 2, load_in_8bit=False, previous_checkpoint=""):
  
    # Create tokenizer and model based on the model_name passed in
    t5_tokenizer = T5Tokenizer.from_pretrained(model_name)
    t5_model = TFT5ForConditionalGeneration.from_pretrained(model_name)
  
    # Open .JSON file
    f_train = open(train_file)
    f_valid = open(valid_file)

    # Read .JSON file to JSON object
    js_train = json.load(f_train)
    js_valid = json.load(f_valid)
  
    # Close JSON file
    f_train.close()
    f_valid.close()
  
    # Get number of text pairs for train and valid set.
    n_train_pairs = len(js_train) #154876
    n_valid_pairs = len(js_valid) #12576
  
    del js_train
    del js_valid
  
    train_data_generator = MultihopQADataGenerator(
        tokenizer=t5_tokenizer,
        model=t5_model,
        n_examples=n_train_pairs,
        data_filename=train_file,
        max_length=max_length,
        batch_size=batch_size
      )
    
    valid_data_generator = MultihopQADataGenerator(
        tokenizer=t5_tokenizer,
        model=t5_model,
        n_examples=n_valid_pairs,
        data_filename=valid_file,
        max_length=max_length,
        batch_size=batch_size
    )
  
    model_wrapper = build_t5_training_wrapper_model(t5_model, max_length)
    
    if previous_checkpoint != "":
        model_wrapper.load_weights(previous_checkpoint)

    model_checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(
        filepath=checkpoint_filepath,
        save_weights_only=True,
        save_freq=1000)
  
    model_wrapper.fit(train_data_generator,
                      validation_data=valid_data_generator,
                      epochs=epochs,
                      callbacks=[model_checkpoint_callback])
  
    return model_wrapper


# TODO disambiguate from evaluation.extract_answer
def extract_answer_from_target(target: str)-> str:
    """
    Find the exact text of the answer given a target with the format "answer is <answer>."
    """
    
    pattern = r'answer is\s*([^\.]+)\.'

    match = re.search(pattern, target)
    result = None
    if match:
        result = match.group(1).strip()
    
    return result


def verify_answer_in_prompt(train_file: str, valid_file: str):
    """
    Need to verify the answers in the prompt so that the question can be answered
    """
    
    # load each file of prompts and targets
    for file in [train_file, valid_file]:
        file = open(file)
        js_file = json.load(file)
        file.close()
        
        filtered_js_file = []
        # only keep the examples with the answer in the prompt
        for example in js_file:
            if extract_answer_from_target(example['target']) in example['prompt']:
                filtered_js_file.append(example)

        # Serializing json
        filtered_js_file = json.dumps(filtered_js_file)
        
        # create new file name, basically append answer verified at the end.
        new_file = file.replace(".json", "") + '_' + str('answer_verified') + '.json'
        
        # Writing to sample.json
        with open(new_file, 'w') as outfile:
            outfile.write(filtered_js_file)


def filter_token_size(train_file, valid_file, token_size):
    f_train = open(train_file)
    f_valid = open(valid_file)
    
    js_train = json.load(f_train)
    js_valid = json.load(f_valid)
    
    # Close JSON file
    f_train.close()
    f_valid.close()
    
    filtered_train = [
        dictionary for dictionary in js_train
        if dictionary['num_prompt_tokens'] <= token_size
        ]
        
    filtered_dev = [
        dictionary for dictionary in js_valid
        if dictionary['num_prompt_tokens'] <= token_size
        ]
        
    # Serializing json
    train_object = json.dumps(filtered_train)
    valid_object = json.dumps(filtered_dev)
    
    # create new file name, basically append token_size at the end.
    f_new_train = train_file.replace(".json", "") + '_' + str(token_size) + '.json'
    f_new_dev = valid_file.replace(".json", "") + '_' + str(token_size) + '.json'
    
    # Writing to sample.json
    with open(f_new_train, 'w') as outfile:
        outfile.write(train_object)
        
    with open(f_new_dev, 'w') as outfile:
        outfile.write(valid_object)