from __future__ import absolute_import, division, print_function

import os
import numpy as np
import theano
import theano.tensor as T
import pickle
from theano.tensor.nnet import conv2d
from ..laplacian_pyr import downsample_img, normalize_lowpass_subt
from ..layers import layers
from .model_basis import ModelBasis
from .model_record import Record


class Model(ModelBasis):
    def __init__(self, model_config, rng=None):
        super(Model, self).__init__(model_config, rng)
        self.set_configs(model_config)

        print('\nFR-VQA sensitivity learn ver.1.0')
        print(' - Model file: %s' % (os.path.split(__file__)[1]))
        print(' - Ignore border: %d' % (self.ign))
        print(' - Loss weights: sens=%.2e' % (self.wl_subj))
        print(' - Regul. weights: L2=%.2e, TV=%.2e' % (
            self.wr_l2, self.wr_tv))

        self.init_model()

    def set_configs(self, model_config):
        self.set_opt_configs(model_config)
        self.wl_subj = float(model_config.get('wl_subj', 1e3))
        self.wr_l2 = float(model_config.get('wr_l2', 5e-3))
        self.wr_tv = float(model_config.get('wr_tv', 1e-2))
        self.ign = int(model_config.get('ign', 4))

    def init_model(self):
        print('\n - Sensitivity map encoder layers')
        key = 'sens_map'
        self.layers[key] = []

        #######################################################################

        self.layers[key].append(layers.ConvLayer(
            self.input_shape, 32, (3, 3), layers.lrelu, name=key + '/conv1_1'))

        self.layers[key].append(layers.ConvLayer(
            self.last_sh(key), 32, (3, 3), layers.lrelu,
            subsample=(2, 2), name=key + '/conv2_1'))

        self.layers[key].append(layers.ConvLayer(
            self.input_shape, 32, (3, 3), layers.lrelu, name=key + '/conv1_2'))

        self.layers[key].append(layers.ConvLayer(
            self.last_sh(key), 32, (3, 3), layers.lrelu,
            subsample=(2, 2), name=key + '/conv2_2'))

        self.layers[key].append(layers.ConvLayer(
            self.input_shape_diff, 8, (3, 3), layers.lrelu, name=key + '/conv1_3'))

        self.layers[key].append(layers.ConvLayer(
            self.last_sh(key), 8, (3, 3), layers.lrelu,
            subsample=(2, 2), name=key + '/conv2_3'))

        self.layers[key].append(layers.ConvLayer(
            self.input_shape_diff, 8, (3, 3), layers.lrelu, name=key + '/conv1_4'))

        self.layers[key].append(layers.ConvLayer(
            self.last_sh(key), 8, (3, 3), layers.lrelu,
            subsample=(2, 2), name=key + '/conv2_4'))

        prev_sh = self.last_sh(key)
        concat_sh = (prev_sh[0], 80) + prev_sh[2:]

        self.layers[key].append(layers.ConvLayer(
            concat_sh, 64, (3, 3), layers.lrelu, name=key + '/conv3'))

        self.layers[key].append(layers.ConvLayer(
            self.last_sh(key), 64, (3, 3), layers.lrelu,
            subsample=(2, 2), name=key + '/conv4'))

        self.layers[key].append(layers.ConvLayer(
            self.last_sh(key), 64, (3, 3), layers.lrelu, name=key + '/conv5'))

        self.layers[key].append(layers.ConvLayer(
            self.last_sh(key), self.num_ch, (3, 3), layers.lrelu,
            b=np.ones((self.num_ch,), dtype='float32'), name=key + '/conv6'))

        #######################################################################
        print('\n - Regression mos layers')
        key = 'reg_mos'
        self.layers[key] = []

        self.layers[key].append(layers.FCLayer(
            self.num_ch, 4, layers.lrelu, name=key + '/fc1'))

        self.layers[key].append(layers.FCLayer(
            self.last_sh(key), 1, lambda x: x, name=key + '/fc2'
        ))

        #######################################################################
        # Sobel filters
        sobel_y_val = np.array([[1, 2, 1], [0, 0, 0], [-1, -2, -1]],
                               dtype='float32').reshape((1, 1, 3, 3))
        self.sobel_y = theano.shared(sobel_y_val, borrow=True)

        sobel_x_val = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]],
                               dtype='float32').reshape((1, 1, 3, 3))
        self.sobel_x = theano.shared(sobel_x_val, borrow=True)

        #######################################################################

        super(Model, self).make_param_list()
        super(Model, self).show_num_params()

    def sobel(self, x, n_ch=1):
        """Apply Sobel operators and returns results in x and y directions"""
        if n_ch > 1:
            y_grads = []
            x_grads = []
            for ch in range(n_ch):
                cur_in = x[:, ch, :, :].dimshuffle(0, 'x', 1, 2)
                y_grads.append(conv2d(cur_in, self.sobel_y,
                                      filter_shape=(1, 1, 3, 3)))
                x_grads.append(conv2d(cur_in, self.sobel_x,
                                      filter_shape=(1, 1, 3, 3)))
            y_grad = T.concatenate(y_grads, axis=1)
            x_grad = T.concatenate(x_grads, axis=1)
        else:
            y_grad = conv2d(x, self.sobel_y, filter_shape=(1, 1, 3, 3))
            x_grad = conv2d(x, self.sobel_x, filter_shape=(1, 1, 3, 3))
        return y_grad, x_grad

    def get_total_variation(self, x, beta=1.5):
        """
        Calculate total variation of the input.
        Arguments
            x: 4D tensor image. It must have 1 channel feauture
        """
        y_grad, x_grad = self.sobel(x, self.num_ch)
        tv = T.mean((y_grad ** 2 + x_grad ** 2) ** (beta / 2))
        return tv

    def log_diff_fn(self, in_a, in_b, eps=0.1):
        diff = 255.0 * (in_a - in_b)
        log_255_sq = np.float32(2 * np.log(255.0))

        val = log_255_sq - T.log(diff ** 2 + eps)
        max_val = np.float32(log_255_sq - np.log(eps))
        return val / max_val

    def power_diff_fn(self, in_a, in_b, power=0.2):
        diff = 255.0 * (in_a - in_b)

        val = T.abs_(diff) ** power
        max_val = np.float32(255.0 ** power)
        return val / max_val

    def sens_map_fn(self, x_set, x_c_set, x_d_diff_set, x_r_diff_set): #large batch comes
        pred_map_0 = T.zeros((x_set.shape[1], x_set.shape[2], 28, 28), 'float32')
        sens_map_0 = T.zeros((x_set.shape[1], x_set.shape[2], 28, 28), 'float32')
        tv_scan_0 = T.zeros(1, 'float32')

        [pred_map, sens_map, tv_scan], _ = theano.reduce(
            self._sens_map_fn_wrapper,
            sequences=[x_set, x_c_set, x_d_diff_set, x_r_diff_set],
            outputs_info=[pred_map_0, sens_map_0, tv_scan_0]
            )

        pred_map /= T.cast(x_set.shape[0], 'float32')
        sens_map /= T.cast(x_set.shape[0], 'float32')
        tv_scan /= T.cast(x_set.shape[0], 'float32')
        return pred_map, sens_map, tv_scan

    def _sens_map_fn_wrapper(self, x_set_minibatch, x_c_set_minibatch,
                             x_d_diff_set_minibatch, x_r_diff_set_minibatch,
                             prev_pred_map, prev_sense_map, prev_tv_scan):
        pred_map, sense_map, tv_scan = self._sens_map_fn(x_set_minibatch,
                                                x_c_set_minibatch,
                                                x_d_diff_set_minibatch,
                                                x_r_diff_set_minibatch)
        pred_map = pred_map + prev_pred_map
        sense_map = sense_map + prev_sense_map
        tv_scan = tv_scan + prev_tv_scan

        return pred_map, sense_map, tv_scan

    def _sens_map_fn(self, x_set_mini, x_c_set_mini, x_d_diff_set_mini, x_r_diff_set_mini):

        # Input reference vectors to 4D tensors -> normalization for reference
        x_norm = normalize_lowpass_subt(x_set_mini, 3, self.num_ch)
        # Input frame vectors to 4D tensors -> normalization for distorted
        x_c_norm = normalize_lowpass_subt(x_c_set_mini, 3, self.num_ch)
        # Get error map from normalized maps
        e = self.log_diff_fn(x_norm, x_c_norm, 1.0)
        # downsampling for error map
        e_ds4 = downsample_img(downsample_img(e, self.num_ch), self.num_ch)

        x_d_diff_norm = abs(x_d_diff_set_mini)

        # Get temporal error
        e_diff = x_r_diff_set_mini-x_d_diff_set_mini

        layers = self.layers['sens_map']
        # x_c
        prev_out = layers[0].get_output(x_c_norm)
        x_c_out = layers[1].get_output(prev_out)

        # err
        prev_out = layers[2].get_output(e)
        err_out = layers[3].get_output(prev_out)

        # x_c_diff
        prev_out = layers[4].get_output(x_d_diff_norm)
        x_c_diff_out = layers[5].get_output(prev_out)

        # x_c_diff_err
        prev_out = layers[6].get_output(e_diff)
        x_c_diff_err_out = layers[7].get_output(prev_out)

        prev_out = T.concatenate([x_c_out, err_out, x_c_diff_out, x_c_diff_err_out], axis=1)

        for layer in layers[8:]:
            prev_out = layer.get_output(prev_out)

        sens_map = prev_out
        pred_map = sens_map * e_ds4

        # TV norm regularization
        tv_scan = self.get_total_variation(sens_map, 3.0)

        return pred_map, sens_map, tv_scan

    def regress_mos_fn(self, feat_vec):
        return self.get_key_layers_output(feat_vec, 'reg_mos')

    def shave_border(self, feat_map):
        if self.ign > 0:
            return feat_map[:, :, self.ign:-self.ign, self.ign:-self.ign]
        else:
            return feat_map

    def load(self, filename):
        """Load parameters from file.
        """
        params = self.get_params()

        with open(filename, 'rb') as f:
            newparams = pickle.load(f)

        params = params[:len(newparams)]

        assert len(newparams) == len(params)
        for p, new_p in zip(params, newparams):
            if p.name != new_p.name:
                print((' @ WARNING: Different name - (loaded) %s != %s'
                      % (new_p.name, p.name)))
            new_p_sh = new_p.get_value(borrow=True).shape
            p_sh = p.get_value(borrow=True).shape
            if p_sh != new_p_sh:
                print(' @ WARNING: Different shape %s - (loaded)' % new_p.name,
                      new_p_sh, end='')
                print(' !=', p_sh)
                continue
            p.set_value(new_p.get_value())
        print(' = Load all params: %s ' % (filename))


    def cost_vqa(self, x, x_c, x_d_diff, x_r_diff, mos, n_img=None, bat2img_idx_set=None):
        """
        Get cost: regression onto MOS using both ref. adn dis. images
        """
        records = Record()

        x_set = x
        x_c_set = x_c
        x_d_diff_set = x_d_diff
        x_r_diff_set = x_r_diff

        pred_map, sens_map, tv_scan = self.sens_map_fn(x_set, x_c_set, x_d_diff_set, x_r_diff_set)

        pred_crop = self.shave_border(pred_map)

        feat_vec = T.mean(pred_crop)
        # regress onto MOS
        mos_p = self.regress_mos_fn(feat_vec).flatten()
        ######################################################################
        # MOS loss
        subj_loss = self.get_mse(mos_p, mos)
        
        # L2 regularization
        l2_reg = self.get_l2_regularization(
            ['sens_map', 'reg_mos'], mode='sum')

        # TV norm regularization
        tv = T.mean(tv_scan)

        # final cost
        cost = self.add_all_weighted_losses(
            [subj_loss, tv, l2_reg],
            [self.wl_subj, self.wr_tv, self.wr_l2])

        # Data to record
        records.add_data('subj', subj_loss * self.wl_subj)
        records.add_data('tv', tv)
        records.add_im_data('mos_p', mos_p)
        records.add_im_data('mos_gt', mos)
        # records.add_imgs('x_c', x_c_im, caxis=[-0.5, 0.5], scale=1.0)
        # records.add_imgs('e_ds', e_ds4, caxis=[0, 1.0], scale=0.25)
        # records.add_imgs('sens_map', sens_map, caxis=[0, 1.5], scale=0.25)
        # records.add_imgs('pred_map', pred_map, caxis=[0, 1.5], scale=0.25)
        return cost, records

    def cost_updates_vqa(self, x, x_c, x_d_diff, x_r_diff, mos, n_img=None, bat2img_idx_set=None):
        cost, records = self.cost_vqa(
            x, x_c, x_d_diff, x_r_diff, mos, n_img=n_img, bat2img_idx_set=bat2img_idx_set)
        updates = self.get_updates_keys(
            cost, ['sens_map', 'reg_mos'])
        return cost, updates, records

    def image_vec_to_tensor(self, input):
        """Reshape input into 4D tensor"""
        return input.dimshuffle(0, 3, 1, 2)

    def set_training_mode(self, training):
        # Decide behaviors of the model during training
        # Batch normalization
        l_keys = [key for key in list(self.layers.keys())]
        self.set_batch_norm_update_averages(False, l_keys)
        self.set_batch_norm_training(True, l_keys)

        # Dropout
        self.set_dropout_on(training)
